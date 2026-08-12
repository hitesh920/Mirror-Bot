"""Cloudflare R2 status, search, and deletion commands."""

import asyncio
import logging
import secrets
from html import escape

from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..app import LOGGER, app, config, owner_filter, r2_service
from ..core.logging_config import log_event
from ..services.cloudflare_analytics import r2_account_usage
from ..services.r2.catalog import (
    DeletePlan,
    execute_delete_plan,
    key_from_input,
    prepare_delete_all,
    prepare_delete_scope,
    search_page,
    storage_stats,
)
from ..services.r2.retention import format_retention
from ..services.status import human_size
from ..telegram.state import ExpiringStore

pending_deletes = ExpiringStore[DeletePlan](ttl_seconds=120, max_items=16)
pending_searches = ExpiringStore[dict](ttl_seconds=300, max_items=8)
SEARCH_MESSAGE_LIMIT = 3_800
SEARCH_PAGE_SIZE = 5


def period_date(value) -> str:
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def delete_buttons(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Permanently delete", callback_data=f"r2del:{token}"
                ),
                InlineKeyboardButton("Cancel", callback_data=f"r2cancel:{token}"),
            ]
        ]
    )


def search_result_line(index: int, item: dict) -> str:
    name = item["name"]
    kind = "Folder" if item.get("kind") == "folder" else "File"
    size = human_size(int(item.get("Size") or 0))
    if item["url"]:
        return (
            f'{index}. <a href="{escape(item["url"], quote=True)}">'
            f"{escape(name[:100])}</a> — <code>{kind} · {size}</code>"
        )
    return (
        f"{index}. <code>{escape(name[:100])}</code> — "
        f"<code>{kind} · {size}</code>\n"
        "<i>Original link unavailable for this older upload.</i>"
    )


def search_result_messages(results: list[dict], list_all: bool) -> list[str]:
    label = "Cloudflare R2 uploads" if list_all else "Cloudflare R2 results"
    header = f"<b>{label}:</b> <code>{len(results)}</code>"
    continuation = f"<b>{label} continued</b>"
    messages: list[str] = []
    current = [header]
    for index, item in enumerate(results, 1):
        line = search_result_line(index, item)
        if len("\n".join([*current, line])) > SEARCH_MESSAGE_LIMIT:
            messages.append("\n".join(current))
            current = [continuation]
        current.append(line)
    messages.append("\n".join(current))
    return messages


def search_page_buttons(token: str, page: int, has_previous: bool, has_next: bool):
    buttons = []
    if has_previous:
        buttons.append(
            InlineKeyboardButton(
                "Previous",
                callback_data=f"r2page:{token}:{page - 1}",
            )
        )
    if has_next:
        buttons.append(
            InlineKeyboardButton(
                "Next",
                callback_data=f"r2page:{token}:{page + 1}",
            )
        )
    return InlineKeyboardMarkup([buttons]) if buttons else None


def search_page_text(page, list_all: bool) -> str:
    label = "Cloudflare R2 uploads" if list_all else "Cloudflare R2 results"
    total_pages = max(1, (page.total + page.page_size - 1) // page.page_size)
    lines = [
        f"<b>{label}:</b> <code>{page.total}</code>",
        f"<b>Page:</b> <code>{page.page + 1} / {total_pages}</code>",
    ]
    for index, item in enumerate(
        page.items,
        page.page * page.page_size + 1,
    ):
        line = search_result_line(index, item)
        if len("\n".join([*lines, line])) > SEARCH_MESSAGE_LIMIT:
            omitted = len(page.items) - (index - page.page * page.page_size - 1)
            lines.append(f"<i>{omitted} result(s) omitted from this message.</i>")
            break
        lines.append(line)
    return "\n".join(lines)


@app.on_message(filters.command("r2stats") & owner_filter)
async def r2stats(_, message: Message):
    if r2_service is None:
        await message.reply("Cloudflare R2 is not configured.")
        return
    progress = await message.reply("Checking Cloudflare R2...")
    try:
        result = await asyncio.to_thread(storage_stats, r2_service)
    except Exception as exc:
        LOGGER.exception("Cloudflare R2 stats failed")
        await progress.edit_text(
            f"Cloudflare R2 check failed:\n{exc}",
            parse_mode=ParseMode.DISABLED,
        )
        return
    analytics = None
    if config.cloudflare_analytics_configured:
        try:
            analytics = await asyncio.to_thread(r2_account_usage, config)
        except RuntimeError:
            LOGGER.exception("Cloudflare R2 account analytics failed")
    lines = [
        "<b>Cloudflare R2</b>",
        f"<b>Bucket:</b> <code>{escape(config.r2_bucket)}</code>",
        f"<b>Prefix:</b> <code>{escape(config.r2_prefix)}</code>",
        f"<b>Objects:</b> <code>{result.objects}</code>",
        f"<b>Stored:</b> <code>{human_size(result.bytes)}</code>",
    ]
    if analytics is not None:
        currency = analytics["currency"]
        symbol = "$" if currency == "USD" else f"{currency} "
        lines.extend(
            [
                "",
                "<b>Current billing period</b>",
                (
                    f"<b>Period:</b> <code>{period_date(analytics['period_start'])}"
                    f" – {period_date(analytics['period_end'])}</code>"
                ),
                (
                    f"<b>Bucket Class A operations:</b> "
                    f"<code>{analytics['class_a']:,}</code>"
                ),
                (
                    f"<b>Bucket Class B operations:</b> "
                    f"<code>{analytics['class_b']:,}</code>"
                ),
                (
                    f"<b>Bucket storage (analytics):</b> "
                    f"<code>{human_size(analytics['bytes'])}</code>"
                ),
                (
                    f"<b>Account R2 billable usage:</b> "
                    f"<code>{symbol}{analytics['billable_cost']:.2f}</code>"
                ),
            ]
        )
    elif config.cloudflare_analytics_configured:
        lines.extend(["", "<i>Account usage analytics is temporarily unavailable.</i>"])
    lines.append(
        f"<b>Automatic deletion:</b> "
        f"<code>{format_retention(config.r2_auto_delete_seconds)}</code>"
    )
    await progress.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("search") & owner_filter)
async def search(_, message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "Usage: <code>/search &lt;name|*&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    query = parts[1].strip()
    list_all = query == "*"
    if r2_service is None:
        await message.reply("Cloudflare R2 is not configured.")
        return
    progress = await message.reply("Searching Cloudflare R2...")
    try:
        result_page = await asyncio.to_thread(
            search_page,
            r2_service,
            query,
            page_size=SEARCH_PAGE_SIZE,
        )
    except Exception as exc:
        LOGGER.exception("Cloudflare R2 search failed")
        await progress.edit_text(
            f"Cloudflare R2 search failed:\n{exc}",
            parse_mode=ParseMode.DISABLED,
        )
        return
    if not result_page.items:
        await progress.edit_text("No Cloudflare R2 results found.")
        return
    token = secrets.token_urlsafe(9)
    pending_searches.put(
        token,
        {
            "snapshot": result_page.snapshot,
            "query": query,
            "list_all": list_all,
            "chat_id": progress.chat.id,
            "message_id": progress.id,
        },
    )
    await progress.edit_text(
        search_page_text(result_page, list_all),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=search_page_buttons(
            token,
            result_page.page,
            result_page.has_previous,
            result_page.has_next,
        ),
    )


@app.on_callback_query(filters.regex(r"^r2page:"))
async def paginate_search(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    _, token, requested = query.data.split(":", 2)
    state = pending_searches.get(token)
    if state is None:
        await query.answer("Expired search", show_alert=True)
        return
    if (
        query.message.chat.id != state["chat_id"]
        or query.message.id != state["message_id"]
    ):
        await query.answer("Invalid search message", show_alert=True)
        return
    await query.answer()
    try:
        requested_page = max(0, int(requested))
        result_page = await asyncio.to_thread(
            search_page,
            r2_service,
            state["query"],
            page=requested_page,
            page_size=SEARCH_PAGE_SIZE,
            snapshot=state["snapshot"],
        )
    except Exception as exc:
        LOGGER.exception("Cloudflare R2 search page failed")
        await query.message.edit_text(
            f"Cloudflare R2 search failed:\n{exc}",
            parse_mode=ParseMode.DISABLED,
        )
        return
    if not result_page.items:
        return
    pending_searches.put(token, state)
    await query.message.edit_text(
        search_page_text(result_page, state["list_all"]),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=search_page_buttons(
            token,
            result_page.page,
            result_page.has_previous,
            result_page.has_next,
        ),
    )


@app.on_message(filters.command("delete") & owner_filter)
async def delete(_, message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "Usage: <code>/delete &lt;key-or-link|all&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    target = parts[1].strip()
    if r2_service is None:
        await message.reply("Cloudflare R2 is not configured.")
        return
    token = secrets.token_urlsafe(12)
    if target.casefold() == "all":
        try:
            plan = await asyncio.to_thread(prepare_delete_all, r2_service)
        except Exception as exc:
            await message.reply(
                f"Cloudflare R2 lookup failed:\n{exc}",
                parse_mode=ParseMode.DISABLED,
            )
            return
        if not plan.keys:
            await message.reply("No inactive Cloudflare R2 objects found.")
            return
        pending_deletes.put(token, plan)
        await message.reply(
            "<b>Confirm Cloudflare R2 cleanup</b>\n"
            f"<b>Objects:</b> <code>{plan.objects}</code>\n"
            f"<b>Stored:</b> <code>{human_size(plan.bytes)}</code>\n\n"
            f"This confirmation-time snapshot under "
            f"<code>{escape(config.r2_prefix)}</code> will be permanently deleted. "
            "Active and later uploads are preserved; the bucket is preserved.",
            parse_mode=ParseMode.HTML,
            reply_markup=delete_buttons(token),
        )
        return
    try:
        key = key_from_input(r2_service, target)
        plan = await asyncio.to_thread(prepare_delete_scope, r2_service, key)
    except Exception as exc:
        await message.reply(
            f"Cloudflare R2 object lookup failed:\n{exc}",
            parse_mode=ParseMode.DISABLED,
        )
        return
    pending_deletes.put(token, plan)
    await message.reply(
        "<b>Confirm Cloudflare R2 delete</b>\n"
        f"<b>Name:</b> <code>{escape(plan.name)}</code>\n"
        f"<b>Type:</b> <code>{escape(plan.kind)}</code>\n"
        f"<b>Objects:</b> <code>{plan.objects}</code>\n"
        f"<b>Size:</b> <code>{human_size(plan.bytes)}</code>\n"
        f"<b>Key:</b> <code>{escape(key)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=delete_buttons(token),
    )


@app.on_callback_query(filters.regex(r"^r2(?:del|cancel):"))
async def confirm_r2_delete(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    action, token = query.data.split(":", 1)
    pending = pending_deletes.take(token)
    if pending is None:
        await query.answer("Expired delete request", show_alert=True)
        await query.message.edit("Cloudflare R2 delete request expired.")
        return
    if action == "r2cancel":
        await query.answer("Cancelled")
        await query.message.edit("Cloudflare R2 delete cancelled.")
        return
    await query.answer("Deleting")
    await query.message.edit("Deleting Cloudflare R2 object(s)...")
    try:
        summary = await asyncio.to_thread(
            execute_delete_plan,
            r2_service,
            pending,
        )
    except Exception as exc:
        LOGGER.exception("Cloudflare R2 deletion failed")
        await query.message.edit(
            f"Cloudflare R2 deletion failed:\n{exc}",
            parse_mode=ParseMode.DISABLED,
        )
        return
    log_event(
        LOGGER,
        logging.INFO,
        "command.r2_delete",
        result="deleted",
        objects=summary.deleted,
    )
    await query.message.edit(
        "<b>Cloudflare R2 deletion complete</b>\n"
        f"<b>Deleted:</b> <code>{summary.deleted}</code>\n"
        f"<b>Skipped active:</b> <code>{summary.skipped_active}</code>",
        parse_mode=ParseMode.HTML,
    )
