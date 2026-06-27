"""Add command and destination-selection handlers."""

from html import escape

from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from ..app import (
    ADD_USAGE, LOGGER, answer_expired_selection, app, config, destination_buttons,
    launch_selected_task, local_buttons, owner_filter, pending_adds,
    start_pending_add_expiry, ytdlp_audio_buttons, ytdlp_buttons,
    ytdlp_video_buttons,
)
from ..core.models import Destination, Source, SourceType
from ..core.parser import parse_add_text
from ..core.source_detector import detect_source


@app.on_message(filters.command("add") & owner_filter)
async def add(_, message: Message):
    try:
        link, options = parse_add_text(message.text or "")
    except ValueError as exc:
        await message.reply(
            f"{escape(str(exc))}\n\n{ADD_USAGE}",
            parse_mode=ParseMode.HTML,
        )
        return
    reply = message.reply_to_message
    source = None
    LOGGER.info(
        "Received /add message_id=%s reply=%s flags=zip:%s extract:%s custom_name:%s",
        message.id,
        bool(reply),
        options.zip,
        options.extract,
        bool(options.name),
    )

    if reply and not link:
        media = reply.document or reply.video or reply.audio or reply.photo or reply.animation
        if media:
            filename = getattr(media, "file_name", "") or ""
            source_type = (
                SourceType.TORRENT_FILE
                if filename.lower().endswith(".torrent")
                else SourceType.TELEGRAM_FILE
            )
            source = Source(source_type, "", filename)
        elif reply.text:
            link = reply.text.split()[0]

    if source is None:
        if not link:
            await message.reply(ADD_USAGE, parse_mode=ParseMode.HTML)
            return
        source = detect_source(link)

    if source.type == SourceType.UNSUPPORTED:
        await message.reply(
            "Unsupported source. Send a supported URL, magnet, Google Drive link, "
            "or reply to a Telegram file/link."
        )
        return

    LOGGER.info("Prepared /add message_id=%s source=%s", message.id, source.type.value)
    token = str(message.id)
    pending_adds[token] = (source, options, reply)
    if source.type == SourceType.YTDLP:
        prompt = await message.reply(
            "Choose download type:",
            reply_markup=ytdlp_buttons(token),
        )
    else:
        prompt = await message.reply(
            "Choose destination:",
            reply_markup=destination_buttons(token),
        )
    start_pending_add_expiry(token, prompt)


@app.on_callback_query(filters.regex(r"^ytkind:"))
async def ytdlp_kind_choice(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    _, kind, token = query.data.split(":", 2)
    if token not in pending_adds:
        await answer_expired_selection(query)
        return
    if kind == "video":
        await query.message.edit(
            "Choose video resolution:",
            reply_markup=ytdlp_video_buttons(token),
        )
    elif kind == "audio":
        await query.message.edit(
            "Choose MP3 quality:",
            reply_markup=ytdlp_audio_buttons(token),
        )
    else:
        await query.message.edit(
            "Choose download type:",
            reply_markup=ytdlp_buttons(token),
        )


@app.on_callback_query(filters.regex(r"^yt:"))
async def ytdlp_choice(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    _, kind, quality, token = query.data.split(":", 3)
    pending = pending_adds.get(token)
    if pending is None:
        await answer_expired_selection(query)
        return
    source, options, reply = pending
    options.ytdlp_kind = kind
    options.ytdlp_quality = quality
    pending_adds[token] = (source, options, reply)
    await query.message.edit("Choose destination:", reply_markup=destination_buttons(token))


@app.on_callback_query(filters.regex(r"^dest:"))
async def destination_choice(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    _, dest, token = query.data.split(":", 2)
    if token not in pending_adds:
        await answer_expired_selection(query)
        return
    if dest == "local":
        await query.message.edit("Choose local category:", reply_markup=local_buttons(token))
        return
    if dest == "telegram":
        await launch_selected_task(query, token, Destination.TELEGRAM)
        return
    if dest == "gdrive":
        await launch_selected_task(query, token, Destination.GOOGLE_DRIVE)
        return
    if dest == "buzzheavier":
        await launch_selected_task(query, token, Destination.BUZZHEAVIER)
        return
    await query.answer("Unknown destination", show_alert=True)


@app.on_callback_query(filters.regex(r"^local:"))
async def local_choice(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    _, category, token = query.data.split(":", 2)
    destination = Destination.LOCAL_MOVIES if category == "movies" else Destination.LOCAL_SERIES
    await launch_selected_task(query, token, destination)
