"""Temporary local file-explorer handler."""

import logging

from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..app import LOGGER, app, get_file_explorer, owner_filter
from ..core.logging_config import log_event


@app.on_message(filters.command("local") & owner_filter)
async def local_explorer_cmd(_, message: Message):
    log_event(LOGGER, logging.INFO, "command.local", result="requested")
    try:
        url = await get_file_explorer().create(message.chat.id)
    except Exception as exc:
        LOGGER.exception("Could not create local file explorer")
        await message.reply(f"Could not create local file explorer:\n{exc}", parse_mode=ParseMode.DISABLED)
        return
    await message.reply(
        "Local file explorer expires in <code>5 minutes</code>.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open local files", url=url)]]),
        disable_web_page_preview=True,
    )
    log_event(LOGGER, logging.INFO, "command.local", result="created")
