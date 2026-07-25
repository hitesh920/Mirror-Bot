"""Cloudflare R2 status, search, and deletion commands."""

import asyncio
import logging
import secrets
from html import escape

from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..app import LOGGER, app, config, owner_filter
from ..core.logging_config import log_event
from ..services.r2_delivery import (
    delete_keys,
    delete_prefix,
    delete_scope,
    key_from_input,
    search_objects,
    storage_stats,
)
from ..services.status import human_size, human_time
from ..telegram.state import ExpiringStore

pending_deletes = ExpiringStore[dict](ttl_seconds=120)


def delete_buttons(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Permanently delete", callback_data=f"r2del:{token}"),
            InlineKeyboardButton("Cancel", callback_data=f"r2cancel:{token}"),
        ]]
    )


@app.on_message(filters.command("r2stats") & owner_filter)
async def r2stats(_, message: Message):
    if not config.r2_configured:
        await message.reply("Cloudflare R2 is not configured.")
        return
    progress = await message.reply("Checking Cloudflare R2...")
    try:
        result = await asyncio.to_thread(storage_stats, config)
    except Exception as exc:
        LOGGER.exception("Cloudflare R2 stats failed")
        await progress.edit_text(
            f"Cloudflare R2 check failed:\n{exc}",
            parse_mode=ParseMode.DISABLED,
        )
        return
    await progress.edit_text(
        "<b>Cloudflare R2</b>\n"
        f"<b>Bucket:</b> <code>{escape(config.r2_bucket)}</code>\n"
        f"<b>Prefix:</b> <code>{escape(config.r2_prefix)}</code>\n"
        f"<b>Objects:</b> <code>{result['objects']}</code>\n"
        f"<b>Stored:</b> <code>{human_size(result['bytes'])}</code>\n"
        f"<b>Automatic deletion:</b> <code>{human_time(config.r2_auto_delete_seconds)}</code>",
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("search") & owner_filter)
async def search(_, message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "Usage: <code>/search &lt;name&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    progress = await message.reply("Searching Cloudflare R2...")
    try:
        results = await asyncio.to_thread(
            search_objects,
            config,
            parts[1].strip(),
            5,
        )
    except Exception as exc:
        LOGGER.exception("Cloudflare R2 search failed")
        await progress.edit_text(
            f"Cloudflare R2 search failed:\n{exc}",
            parse_mode=ParseMode.DISABLED,
        )
        return
    if not results:
        await progress.edit_text("No Cloudflare R2 results found.")
        return
    lines = [f"<b>Cloudflare R2 results:</b> <code>{len(results)}</code>"]
    for index, item in enumerate(results, 1):
        name = item["name"]
        kind = "Folder" if item.get("kind") == "folder" else "File"
        size = human_size(int(item.get("Size") or 0))
        if item["url"]:
            lines.append(
                f'{index}. <a href="{escape(item["url"], quote=True)}">'
                f'{escape(name[:100])}</a> — <code>{kind} · {size}</code>'
            )
        else:
            lines.append(
                f"{index}. <code>{escape(name[:100])}</code> — "
                f"<code>{kind} · {size}</code>\n"
                "<i>Original link unavailable for this older upload.</i>"
            )
    await progress.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
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
    token = secrets.token_urlsafe(12)
    if target.casefold() == "all":
        try:
            stats = await asyncio.to_thread(storage_stats, config)
        except Exception as exc:
            await message.reply(
                f"Cloudflare R2 lookup failed:\n{exc}",
                parse_mode=ParseMode.DISABLED,
            )
            return
        pending_deletes.put(token, {"all": True})
        await message.reply(
            "<b>Confirm Cloudflare R2 cleanup</b>\n"
            f"<b>Objects:</b> <code>{stats['objects']}</code>\n"
            f"<b>Stored:</b> <code>{human_size(stats['bytes'])}</code>\n\n"
            f"Everything under <code>{escape(config.r2_prefix)}</code> will be "
            "permanently deleted. The bucket is preserved.",
            parse_mode=ParseMode.HTML,
            reply_markup=delete_buttons(token),
        )
        return
    try:
        key = key_from_input(config, target)
        item = await asyncio.to_thread(delete_scope, config, key)
    except Exception as exc:
        await message.reply(
            f"Cloudflare R2 object lookup failed:\n{exc}",
            parse_mode=ParseMode.DISABLED,
        )
        return
    pending_deletes.put(token, {"keys": item["keys"]})
    await message.reply(
        "<b>Confirm Cloudflare R2 delete</b>\n"
        f"<b>Name:</b> <code>{escape(item['name'])}</code>\n"
        f"<b>Type:</b> <code>{escape(item['kind'])}</code>\n"
        f"<b>Objects:</b> <code>{item['objects']}</code>\n"
        f"<b>Size:</b> <code>{human_size(item['bytes'])}</code>\n"
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
        if pending.get("all"):
            deleted = await asyncio.to_thread(delete_prefix, config)
        else:
            deleted = await asyncio.to_thread(
                delete_keys,
                config,
                pending["keys"],
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
        objects=deleted,
    )
    await query.message.edit(
        "<b>Cloudflare R2 deletion complete</b>\n"
        f"<b>Deleted:</b> <code>{deleted}</code>",
        parse_mode=ParseMode.HTML,
    )
