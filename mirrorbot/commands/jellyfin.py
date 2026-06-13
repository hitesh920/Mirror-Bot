"""Jellyfin management handlers."""

import asyncio

from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import Message

from ..app import (
    LOGGER, app, config, format_jellyfin_status, jellyfin, jellyfin_api,
    jellyfin_buttons, jellyfin_server_info, jellyfin_status_text, owner_filter,
)


@app.on_message(filters.command("jellyfin") & owner_filter)
async def jellyfin_cmd(_, message: Message):
    LOGGER.info("Received /jellyfin")
    try:
        text = await jellyfin_status_text()
    except Exception as exc:
        LOGGER.exception("Jellyfin status failed")
        await message.reply(
            f"Jellyfin control failed:\n{exc}",
            parse_mode=ParseMode.DISABLED,
        )
        return
    await message.reply(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=jellyfin_buttons(),
        disable_web_page_preview=True,
    )


@app.on_callback_query(filters.regex(r"^jf:"))
async def jellyfin_action(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    action = query.data.split(":", 1)[1]
    LOGGER.info("Received /jellyfin action=%s", action)
    try:
        if action == "scan":
            await asyncio.to_thread(jellyfin_api.scan_library)
            status = await asyncio.to_thread(jellyfin.status)
            label = "Library scan requested"
        elif action == "start":
            status = await asyncio.to_thread(jellyfin.start)
            label = "Started"
        elif action == "stop":
            status = await asyncio.to_thread(jellyfin.stop)
            label = "Stopped"
        elif action == "restart":
            status = await asyncio.to_thread(jellyfin.restart)
            label = "Restarted"
        else:
            status = await asyncio.to_thread(jellyfin.status)
            label = "Status"
    except Exception as exc:
        LOGGER.exception("Jellyfin %s failed", action)
        await query.answer("Jellyfin action failed", show_alert=True)
        await query.message.edit_text(
            f"Jellyfin control failed:\n{exc}",
            parse_mode=ParseMode.DISABLED,
            reply_markup=jellyfin_buttons(),
            disable_web_page_preview=True,
        )
        return
    LOGGER.info("Jellyfin action=%s result=%s state=%s health=%s", action, label, status.state, status.health)
    await query.answer(label)
    try:
        await query.message.edit_text(
            format_jellyfin_status(status, label, await jellyfin_server_info(status)),
            parse_mode=ParseMode.HTML,
            reply_markup=jellyfin_buttons(),
            disable_web_page_preview=True,
        )
    except MessageNotModified:
        LOGGER.debug("Jellyfin %s produced unchanged status message", action)
