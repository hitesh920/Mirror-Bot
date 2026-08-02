"""Add command and destination-selection handlers."""

from html import escape

from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from ..app import (
    ADD_USAGE,
    LOGGER,
    PendingAdd,
    answer_expired_selection,
    app,
    batch_mode_buttons,
    config,
    destination_buttons,
    launch_selected_task,
    owner_filter,
    pending_adds,
    start_pending_add_expiry,
    ytdlp_audio_buttons,
    ytdlp_buttons,
    ytdlp_video_buttons,
)
from ..core.batch import collect_batch_sources
from ..core.models import Destination, Source, SourceType
from ..core.parser import parse_add_text, replied_link
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

    if options.batch_messages:
        if link:
            await message.reply(
                "Batch mode must reply to the first message; do not put a link "
                "inside the /add command."
            )
            return
        if reply is None:
            await message.reply("Reply to the first link message with /add -b.")
            return
        message_ids = list(range(reply.id, reply.id + options.batch_messages))
        if len(message_ids) == 1:
            messages = [reply]
        else:
            try:
                fetched = await app.get_messages(
                    message.chat.id,
                    message_ids=message_ids,
                )
            except Exception:
                LOGGER.exception(
                    "Could not collect batch messages message_id=%s count=%s",
                    message.id,
                    options.batch_messages,
                )
                await message.reply("Could not collect those messages. Try again.")
                return
            if isinstance(fetched, Message):
                fetched_messages = [fetched]
            else:
                fetched_messages = list(fetched)
            fetched_by_id = {
                item.id: item
                for item in fetched_messages
                if item is not None and not getattr(item, "empty", False)
            }
            messages = [fetched_by_id.get(message_id) for message_id in message_ids]
        collection = collect_batch_sources(messages)
        if len(collection.sources) < 2:
            await message.reply(
                "Batch mode needs at least 2 valid direct HTTP links.\n"
                f"Collected: {len(collection.sources)}\n"
                f"Skipped: {collection.skipped}"
            )
            return
        token = str(message.id)
        pending_adds[token] = PendingAdd(
            sources=collection.sources,
            options=options,
            reply=reply,
            skipped=collection.skipped,
        )
        name_note = ""
        if options.name:
            name_note = "\nThe custom -n name applies only to ZIP upload."
        prompt = await message.reply(
            f"Batch links collected: {len(collection.sources)}\n"
            f"Skipped: {collection.skipped}\n\n"
            "Choose how to upload them. Separate uploads keep each original "
            f"filename.{name_note}",
            reply_markup=batch_mode_buttons(token),
        )
        start_pending_add_expiry(token, prompt)
        return

    if reply and not link:
        media = (
            reply.document
            or reply.video
            or reply.audio
            or reply.photo
            or reply.animation
        )
        if media:
            filename = getattr(media, "file_name", "") or ""
            source_type = (
                SourceType.TORRENT_FILE
                if filename.lower().endswith(".torrent")
                else SourceType.TELEGRAM_FILE
            )
            source = Source(source_type, "", filename)
        elif reply.text:
            link = replied_link(reply.text)

    if source is None:
        if not link:
            await message.reply(ADD_USAGE, parse_mode=ParseMode.HTML)
            return
        source = detect_source(link)

    if source.type == SourceType.UNSUPPORTED:
        await message.reply(
            "Unsupported source. Send a supported URL, magnet, "
            "or reply to a Telegram file/link."
        )
        return

    LOGGER.info("Prepared /add message_id=%s source=%s", message.id, source.type.value)
    token = str(message.id)
    pending_adds[token] = PendingAdd([source], options, reply)
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
    pending.options.ytdlp_kind = kind
    pending.options.ytdlp_quality = quality
    await query.message.edit(
        "Choose destination:", reply_markup=destination_buttons(token)
    )


@app.on_callback_query(filters.regex(r"^batch:"))
async def batch_mode_choice(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    _, mode, token = query.data.split(":", 2)
    pending = pending_adds.get(token)
    if pending is None:
        await answer_expired_selection(query)
        return
    if mode not in {"separate", "zip"}:
        await query.answer("Unknown batch mode", show_alert=True)
        return
    pending.batch_mode = mode
    label = "separate uploads" if mode == "separate" else "one ZIP upload"
    await query.message.edit(
        f"Selected {label}. Choose one destination for the batch:",
        reply_markup=destination_buttons(token),
    )
    start_pending_add_expiry(token, query.message)


@app.on_callback_query(filters.regex(r"^dest:"))
async def destination_choice(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    _, dest, token = query.data.split(":", 2)
    if token not in pending_adds:
        await answer_expired_selection(query)
        return
    if dest == "telegram":
        await launch_selected_task(query, token, Destination.TELEGRAM)
        return
    if dest == "r2":
        if not config.r2_configured:
            await query.answer("Cloudflare R2 is not configured", show_alert=True)
            return
        await launch_selected_task(query, token, Destination.CLOUDFLARE_R2)
        return
    await query.answer("Unknown destination", show_alert=True)
