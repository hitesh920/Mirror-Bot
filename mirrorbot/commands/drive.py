"""Google Drive status, search, sharing, and deletion handlers."""

import asyncio
import secrets
from html import escape

from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..app import (
    LOGGER, app, config, delete_google_drive_link, drive_search_pages, owner_filter,
    pending_drive_delete_chats, pending_drive_delete_items,
    drive_share_pages, start_drive_delete_expiry, take_pending_drive_delete,
)
from ..downloaders.gdrive import drive_id_from_url
from ..services.drive_sharing import DriveShareError, build_drive_share
from ..services.google_drive_delivery import (
    delete_drive_item, drive_item_info, drive_storage_quota, load_credentials,
    search_drive_items,
)
from ..services.status import human_size


@app.on_message(filters.command("gdstats") & owner_filter)
async def gdstats(_, message: Message):
    LOGGER.info("Received /gdstats")
    credentials_exists = config.google_credentials_file.is_file()
    token_exists = config.google_token_file.is_file()
    folder_configured = bool(config.google_drive_folder_id)
    lines = [
        "<b>Google Drive stats</b>",
        f"<b>Credentials:</b> <code>{'found' if credentials_exists else 'missing'}</code>",
        f"<b>Token:</b> <code>{'found' if token_exists else 'missing'}</code>",
        f"<b>Upload folder:</b> <code>{'configured' if folder_configured else 'missing'}</code>",
    ]
    if not credentials_exists or not token_exists:
        await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)
        return
    try:
        await asyncio.to_thread(load_credentials, config)
        quota = await asyncio.to_thread(drive_storage_quota, config)
    except Exception as exc:
        lines.append("<b>Auth:</b> <code>failed</code>")
        lines.append(f"<b>Error:</b> <code>{escape(str(exc))}</code>")
        await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    used = int(quota.get("usage") or 0)
    trash = int(quota.get("usageInDriveTrash") or 0)
    limit = int(quota.get("limit") or 0)
    lines.append("<b>Auth:</b> <code>ready</code>")
    lines.append(f"<b>Used:</b> <code>{human_size(used)}</code>")
    if limit:
        lines.append(f"<b>Limit:</b> <code>{human_size(limit)}</code>")
        lines.append(f"<b>Free:</b> <code>{human_size(max(0, limit - used))}</code>")
    else:
        lines.append("<b>Limit:</b> <code>Unlimited</code>")
    lines.append(f"<b>Trash:</b> <code>{human_size(trash)}</code>")
    await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("delete") & owner_filter)
async def delete_cmd(_, message: Message):
    LOGGER.info("Received /delete for Google Drive")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        await delete_google_drive_link(message, parts[1].strip())
        return
    pending_drive_delete_chats.add(message.chat.id)
    await message.reply("Send the Google Drive link or file ID to delete.")


@app.on_message(filters.command("search") & owner_filter)
async def search_cmd(_, message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply("Usage: <code>/search &lt;name&gt;</code>", parse_mode=ParseMode.HTML)
        return
    query_text = parts[1].strip()
    LOGGER.info("Received /search query=%s", query_text)
    progress = await message.reply("Searching Google Drive...")
    try:
        results = await asyncio.to_thread(search_drive_items, config, query_text, 100)
    except Exception as exc:
        LOGGER.exception("Google Drive search failed")
        await progress.edit_text(
            f"Google Drive search failed:\n{exc}",
            parse_mode=ParseMode.DISABLED,
        )
        return
    if not results:
        await progress.edit_text("No Google Drive results found.")
        return
    try:
        url = await drive_search_pages.create(query_text, results)
    except Exception as exc:
        LOGGER.exception("Could not create Google Drive search page")
        await progress.edit_text(
            f"Could not create search page:\n{exc}",
            parse_mode=ParseMode.DISABLED,
        )
        return
    await progress.edit_text(
        f"<b>Found:</b> <code>{len(results)}</code> result(s)\n"
        "The search page expires in <code>5 minutes</code>.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Click here to view results", url=url)]]
        ),
        disable_web_page_preview=True,
    )


@app.on_message(filters.command("share") & owner_filter)
async def share_cmd(_, message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "Usage: <code>/share &lt;public-drive-link&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    link = parts[1].strip()
    LOGGER.info("Received /share")
    progress = await message.reply("Preparing public Google Drive share...")
    try:
        file_id = drive_id_from_url(link)
        manifest = await asyncio.to_thread(build_drive_share, config, file_id)
        LOGGER.info(
            "Verified public Google Drive share id=%s files=%s folders=%s",
            file_id,
            len(manifest.files),
            manifest.folder_count,
        )
        share_url = await drive_share_pages.create(manifest)
    except (DriveShareError, ValueError) as exc:
        LOGGER.warning("Google Drive share rejected error=%s", exc)
        await progress.edit_text(
            f"Could not create public share:\n{exc}",
            parse_mode=ParseMode.DISABLED,
        )
        return
    except Exception as exc:
        LOGGER.exception("Google Drive public share failed")
        await progress.edit_text(
            f"Could not create public share:\n{exc}",
            parse_mode=ParseMode.DISABLED,
        )
        return
    LOGGER.info(
        "Created temporary Drive share id=%s files=%s folders=%s url=%s",
        file_id,
        len(manifest.files),
        manifest.folder_count,
        share_url,
    )
    await progress.edit_text(
        "<b>Public Google Drive share created</b>\n"
        f"<b>Name:</b> <code>{escape(manifest.name)}</code>\n"
        f"<b>Files:</b> <code>{len(manifest.files)}</code>\n"
        f"<b>Folders:</b> <code>{manifest.folder_count}</code>\n"
        "<b>Expires:</b> <code>5 minutes</code>\n\n"
        "Anyone with the temporary link can open this page.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Open Share Page", url=share_url)]]
        ),
        disable_web_page_preview=True,
    )


@app.on_message(filters.text & ~filters.regex(r"^/") & owner_filter)
async def pending_drive_delete(_, message: Message):
    if message.chat.id not in pending_drive_delete_chats:
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    pending_drive_delete_chats.discard(message.chat.id)
    await delete_google_drive_link(message, text)


@app.on_callback_query(filters.regex(r"^dgd"))
async def confirm_drive_delete(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    action, token = query.data.split(":", 1)
    item = take_pending_drive_delete(token)
    if item is None:
        await query.answer("Expired delete request", show_alert=True)
        try:
            await query.message.edit("Delete request expired.")
        except Exception:
            pass
        return
    if action == "dgdcancel":
        await query.answer("Cancelled")
        await query.message.edit("Google Drive delete cancelled.")
        return
    await query.answer("Deleting")
    try:
        deleted = await asyncio.to_thread(delete_drive_item, config, item["id"])
    except Exception as exc:
        LOGGER.exception("Google Drive delete failed")
        await query.message.edit(
            f"Google Drive delete failed:\n{exc}",
            parse_mode=ParseMode.DISABLED,
        )
        return
    LOGGER.info(
        "Deleted Google Drive item id=%s name=%s",
        deleted.get("id"),
        deleted.get("name"),
    )
    await query.message.edit(
        "<b>Google Drive item deleted</b>\n"
        f"<b>Name:</b> <code>{escape(deleted.get('name', 'Untitled'))}</code>\n"
        f"<b>ID:</b> <code>{escape(deleted.get('id', item['id']))}</code>",
        parse_mode=ParseMode.HTML,
    )
