import asyncio
import logging
from collections import defaultdict
from pathlib import Path
from shutil import rmtree

import psutil
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .core.config import Config
from .core.logging_config import setup_logging
from .core.models import Destination, Source, SourceType, TaskPhase
from .core.parser import parse_add_text
from .core.source_detector import detect_source
from .services.status import format_status
from .services.task_manager import TaskManager

setup_logging()
LOGGER = logging.getLogger(__name__)
config = Config.load()
manager = TaskManager(config)
pending_adds: dict[str, tuple[Source, object, Message | None]] = {}
delete_targets: dict[str, Path] = {}
status_messages: dict[int, Message] = {}
status_jobs: dict[int, asyncio.Task] = {}
status_text: dict[int, str] = {}
status_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

app = Client(
    "mirrorbot",
    api_id=config.telegram_api_id,
    api_hash=config.telegram_api_hash,
    bot_token=config.bot_token,
    max_concurrent_transmissions=config.task_limit,
)


def owner_only(_, __, message: Message) -> bool:
    user = message.from_user or message.sender_chat
    return bool(user and user.id == config.owner_id)


owner_filter = filters.create(owner_only)


def chat_tasks(chat_id: int):
    return [task for task in manager.active_tasks() if task.chat_id == chat_id]


async def update_status_message(chat_id: int) -> None:
    async with status_locks[chat_id]:
        tasks = chat_tasks(chat_id)
        if not tasks:
            message = status_messages.pop(chat_id, None)
            status_text.pop(chat_id, None)
            if message:
                try:
                    await message.delete()
                except Exception:
                    pass
            return

        text = format_status(tasks)
        message = status_messages.get(chat_id)
        if message is None:
            status_messages[chat_id] = await app.send_message(
                chat_id, text, parse_mode=ParseMode.HTML, disable_notification=True
            )
            status_text[chat_id] = text
        elif status_text.get(chat_id) != text:
            try:
                await message.edit_text(text, parse_mode=ParseMode.HTML)
                status_text[chat_id] = text
            except Exception:
                LOGGER.exception("Could not update status message chat=%s", chat_id)


async def replace_status_message(chat_id: int) -> None:
    async with status_locks[chat_id]:
        text = format_status(chat_tasks(chat_id))
        new_message = await app.send_message(
            chat_id, text, parse_mode=ParseMode.HTML, disable_notification=True
        )
        old_message = status_messages.get(chat_id)
        status_messages[chat_id] = new_message
        status_text[chat_id] = text
        if old_message:
            try:
                await old_message.delete()
            except Exception:
                pass


async def status_loop(chat_id: int) -> None:
    try:
        while chat_tasks(chat_id):
            await update_status_message(chat_id)
            await asyncio.sleep(config.status_update_interval)
        await update_status_message(chat_id)
    finally:
        status_jobs.pop(chat_id, None)


async def start_live_status(chat_id: int, message: Message) -> None:
    async with status_locks[chat_id]:
        old_message = status_messages.get(chat_id)
        text = format_status(chat_tasks(chat_id))
        await message.edit_text(text, parse_mode=ParseMode.HTML)
        status_messages[chat_id] = message
        status_text[chat_id] = text
        if old_message and old_message.id != message.id:
            try:
                await old_message.delete()
            except Exception:
                pass
    job = status_jobs.get(chat_id)
    if job is None or job.done():
        status_jobs[chat_id] = asyncio.create_task(status_loop(chat_id))


async def send_live_status(chat_id: int) -> None:
    await update_status_message(chat_id)
    job = status_jobs.get(chat_id)
    if job is None or job.done():
        status_jobs[chat_id] = asyncio.create_task(status_loop(chat_id))


def destination_buttons(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Local", callback_data=f"dest:local:{token}")],
            [
                InlineKeyboardButton("Telegram", callback_data=f"dest:telegram:{token}"),
                InlineKeyboardButton("Google Drive", callback_data=f"dest:gdrive:{token}"),
            ],
        ]
    )


def local_buttons(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Movies", callback_data=f"local:movies:{token}"),
                InlineKeyboardButton("Series", callback_data=f"local:series:{token}"),
            ]
        ]
    )


def ytdlp_buttons(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Audio 320kbps MP3", callback_data=f"yt:audio:320:{token}")],
            [
                InlineKeyboardButton("360p", callback_data=f"yt:video:360:{token}"),
                InlineKeyboardButton("480p", callback_data=f"yt:video:480:{token}"),
            ],
            [
                InlineKeyboardButton("720p", callback_data=f"yt:video:720:{token}"),
                InlineKeyboardButton("1080p", callback_data=f"yt:video:1080:{token}"),
            ],
        ]
    )


@app.on_message(filters.command("start") & owner_filter)
async def start(_, message: Message):
    await message.reply("Bot is online. Use /add to start a task.")


@app.on_message(filters.command("help") & owner_filter)
async def help_cmd(_, message: Message):
    await message.reply(
        "/add <link> [-z|-zp pass|-e|-ep pass|-n name]\n"
        "/status\n/stats\n/cancel <task-id>\n/cancelall\n/delete"
    )


@app.on_message(filters.command("add") & owner_filter)
async def add(_, message: Message):
    try:
        link, options = parse_add_text(message.text or "")
    except ValueError as exc:
        await message.reply(str(exc))
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
            await message.reply("Send a link with /add or reply to a Telegram file/link.")
            return
        source = detect_source(link)

    if source.type == SourceType.UNSUPPORTED:
        await message.reply("Unsupported source.")
        return

    LOGGER.info("Prepared /add message_id=%s source=%s", message.id, source.type.value)
    token = str(message.id)
    pending_adds[token] = (source, options, reply)
    if source.type == SourceType.YTDLP:
        await message.reply("Choose yt-dlp download type:", reply_markup=ytdlp_buttons(token))
    else:
        await message.reply("Choose destination:", reply_markup=destination_buttons(token))


@app.on_callback_query(filters.regex(r"^yt:"))
async def ytdlp_choice(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    _, kind, quality, token = query.data.split(":", 3)
    pending = pending_adds.get(token)
    if pending is None:
        await query.answer("Expired task", show_alert=True)
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
    if dest == "local":
        await query.message.edit("Choose local category:", reply_markup=local_buttons(token))
        return
    if dest == "telegram":
        await launch_selected_task(query, token, Destination.TELEGRAM)
        return
    await query.answer("This destination is planned for a later step.", show_alert=True)


async def launch_selected_task(query, token: str, destination: Destination) -> None:
    pending = pending_adds.pop(token, None)
    if pending is None:
        await query.answer("Expired task", show_alert=True)
        return
    source, options, reply = pending
    task = manager.create_task(query.from_user.id, query.message.chat.id, query.message.id, source, destination, options)
    LOGGER.info("Task %s: selected destination=%s", task.short_id(), destination.value)
    is_torrent = source.type in {SourceType.MAGNET, SourceType.TORRENT_FILE}
    if is_torrent:
        await query.message.edit("Collecting torrent metadata...")

    async def runner():
        async def selector_ready(selected_task):
            try:
                await query.message.delete()
            except Exception:
                pass
            return await app.send_message(
                task.chat_id,
                "Torrent files are ready for review.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Click here to review files", url=selected_task.selection_url)]]
                ),
                disable_web_page_preview=True,
            )

        async def selector_done(selector_message):
            try:
                await selector_message.delete()
            except Exception:
                pass
            if task.phase == TaskPhase.DOWNLOADING:
                await send_live_status(task.chat_id)

        await manager.run_task(
            task,
            telegram_reply=reply,
            telegram_client=app,
            on_selector_ready=selector_ready,
            on_selector_done=selector_done,
        )
        if is_torrent:
            try:
                await query.message.delete()
            except Exception:
                pass
        if task.phase.value == "complete":
            if task.destination != Destination.TELEGRAM:
                await app.send_message(task.chat_id, f"Saved locally:\n`{task.result_path}`")
        elif task.error:
            await app.send_message(task.chat_id, f"Task `{task.short_id()}` failed:\n`{task.error}`")
        else:
            await app.send_message(task.chat_id, f"Task `{task.short_id()}` {task.phase.value}.")
        await update_status_message(task.chat_id)

    asyncio.create_task(runner())
    if not is_torrent:
        await asyncio.sleep(0)
        await start_live_status(task.chat_id, query.message)


@app.on_callback_query(filters.regex(r"^local:"))
async def local_choice(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    _, category, token = query.data.split(":", 2)
    destination = Destination.LOCAL_MOVIES if category == "movies" else Destination.LOCAL_SERIES
    await launch_selected_task(query, token, destination)


@app.on_message(filters.command("status") & owner_filter)
async def status(_, message: Message):
    LOGGER.info("Received /status active_tasks=%s", len(manager.active_tasks()))
    if not chat_tasks(message.chat.id):
        await message.reply("No active tasks.")
        return
    await replace_status_message(message.chat.id)
    try:
        await message.delete()
    except Exception:
        pass
    job = status_jobs.get(message.chat.id)
    if job is None or job.done():
        status_jobs[message.chat.id] = asyncio.create_task(status_loop(message.chat.id))


@app.on_message(filters.command("stats") & owner_filter)
async def stats(_, message: Message):
    LOGGER.info("Received /stats active_tasks=%s", len(manager.active_tasks()))
    disk = psutil.disk_usage(str(config.local_download_root))
    await message.reply(
        "Bot stats:\n"
        f"CPU: {psutil.cpu_percent()}%\n"
        f"RAM: {psutil.virtual_memory().percent}%\n"
        f"Local free: {disk.free // (1024 ** 3)} GiB\n"
        f"Tasks: {len(manager.active_tasks())}"
    )


@app.on_message(filters.command("cancel") & owner_filter)
async def cancel(_, message: Message):
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply("Usage: /cancel <task-id>")
        return
    if manager.cancel(parts[1]):
        LOGGER.info("Received /cancel task=%s", parts[1])
        await message.reply("Cancel requested.")
    else:
        await message.reply("Task not found.")


@app.on_message(filters.command("cancelall") & owner_filter)
async def cancel_all(_, message: Message):
    LOGGER.info("Received /cancelall active_tasks=%s", len(manager.active_tasks()))
    for task in manager.active_tasks():
        manager.cancel(task.id)
    await message.reply("Cancel requested for all active tasks.")


@app.on_message(filters.command("delete") & owner_filter)
async def delete_cmd(_, message: Message):
    LOGGER.info("Received /delete")
    await message.reply(
        "Choose delete target:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Local", callback_data="delete:local")]]),
    )


@app.on_callback_query(filters.regex(r"^delete:local$"))
async def delete_local(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    await query.message.edit("Choose local category:", reply_markup=InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Movies", callback_data="dellist:movies:0"),
            InlineKeyboardButton("Series", callback_data="dellist:series:0"),
        ]
    ]))


@app.on_callback_query(filters.regex(r"^dellist:"))
async def delete_list(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    _, category, page_text = query.data.split(":", 2)
    page = int(page_text)
    root = config.local_download_root / category
    root.mkdir(parents=True, exist_ok=True)
    folders = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name.lower())
    chunk = folders[page * 8 : page * 8 + 8]
    buttons = []
    for folder in chunk:
        token = f"{category}:{page}:{len(delete_targets)}"
        delete_targets[token] = folder
        buttons.append([InlineKeyboardButton(folder.name[:60], callback_data=f"delitem:{token}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Back", callback_data=f"dellist:{category}:{page - 1}"))
    if (page + 1) * 8 < len(folders):
        nav.append(InlineKeyboardButton("Next", callback_data=f"dellist:{category}:{page + 1}"))
    if nav:
        buttons.append(nav)
    await query.message.edit(f"{category.title()} folders:", reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


@app.on_callback_query(filters.regex(r"^delitem:"))
async def delete_item(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    _, token = query.data.split(":", 1)
    target = delete_targets.pop(token, None)
    if target is None:
        await query.answer("Expired folder selection", show_alert=True)
        return
    root = config.local_download_root.resolve()
    if root not in target.resolve().parents:
        await query.answer("Invalid path", show_alert=True)
        return
    LOGGER.info("Deleting local folder path=%s", target)
    rmtree(target, ignore_errors=True)
    await query.message.edit(f"Deleted `{target}`")


@app.on_message(filters.command("ping") & owner_filter)
async def ping(_, message: Message):
    LOGGER.info("Received /ping")
    await message.reply("pong")


@app.on_message(filters.command("log") & owner_filter)
async def log_cmd(_, message: Message):
    LOGGER.info("Received /log")
    log_path = Path(config.log_file)
    if log_path.exists():
        await message.reply_document(str(log_path))
    else:
        await message.reply("No log file yet.")


def run():
    config.download_dir.mkdir(parents=True, exist_ok=True)
    (config.local_download_root / "movies").mkdir(parents=True, exist_ok=True)
    (config.local_download_root / "series").mkdir(parents=True, exist_ok=True)
    LOGGER.info("Starting bot")
    app.run()
