import asyncio
import logging
from pathlib import Path
from shutil import rmtree

import psutil
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Config
from .models import Destination, Source, SourceType
from .parser import parse_add_text
from .source_detector import detect_source
from .task_manager import TaskManager

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
    level=logging.INFO,
)

LOGGER = logging.getLogger(__name__)
config = Config.load()
manager = TaskManager(config)
pending_adds: dict[str, tuple[Source, object, Message | None]] = {}
delete_targets: dict[str, Path] = {}

app = Client(
    "mirrorbot",
    api_id=config.telegram_api_id,
    api_hash=config.telegram_api_hash,
    bot_token=config.bot_token,
)


def owner_only(_, __, message: Message) -> bool:
    user = message.from_user or message.sender_chat
    return bool(user and user.id == config.owner_id)


owner_filter = filters.create(owner_only)


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
    link, options = parse_add_text(message.text or "")
    reply = message.reply_to_message
    source = None

    if reply and not link:
        media = reply.document or reply.video or reply.audio or reply.photo or reply.animation
        if media:
            source = Source(SourceType.TELEGRAM_FILE, "", getattr(media, "file_name", "") or "")
        elif reply.text:
            link = reply.text.split()[0]

    if source is None:
        if not link:
            await message.reply("Send a link with /add or reply to a Telegram file/link.")
            return
        source = detect_source(link)

    if source.type in {SourceType.UNSUPPORTED, SourceType.MAGNET, SourceType.TORRENT_FILE, SourceType.GOOGLE_DRIVE, SourceType.RCLONE}:
        planned = {
            SourceType.MAGNET: "Torrent/qBittorrent support is planned for the next build pass.",
            SourceType.TORRENT_FILE: "Torrent/qBittorrent support is planned for the next build pass.",
            SourceType.GOOGLE_DRIVE: "Google Drive download support is planned for the next build pass.",
            SourceType.RCLONE: "rclone download support is planned for the next build pass.",
            SourceType.UNSUPPORTED: "Unsupported source.",
        }
        await message.reply(planned[source.type])
        return

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
    await query.answer("This destination is planned for a later step.", show_alert=True)


@app.on_callback_query(filters.regex(r"^local:"))
async def local_choice(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    _, category, token = query.data.split(":", 2)
    pending = pending_adds.pop(token, None)
    if pending is None:
        await query.answer("Expired task", show_alert=True)
        return
    source, options, reply = pending
    destination = Destination.LOCAL_MOVIES if category == "movies" else Destination.LOCAL_SERIES
    task = manager.create_task(query.from_user.id, query.message.chat.id, query.message.id, source, destination, options)
    await query.message.edit(f"Started local task `{task.short_id()}` for {category}.")

    async def runner():
        await manager.run_local_task(task, reply)
        if task.phase.value == "complete":
            await app.send_message(task.chat_id, f"Saved locally:\n`{task.result_path}`")
        elif task.error:
            await app.send_message(task.chat_id, f"Task `{task.short_id()}` failed:\n`{task.error}`")
        else:
            await app.send_message(task.chat_id, f"Task `{task.short_id()}` {task.phase.value}.")

    asyncio.create_task(runner())


@app.on_message(filters.command("status") & owner_filter)
async def status(_, message: Message):
    reply = await message.reply(format_status())
    while manager.active_tasks():
        await asyncio.sleep(config.status_update_interval)
        try:
            await reply.edit(format_status())
        except Exception:
            break


def format_status() -> str:
    active = manager.active_tasks()
    if not active:
        return "No active tasks."
    lines = ["Active tasks:"]
    for task in active:
        pct = f"{task.progress * 100:.1f}%" if task.progress else "..."
        lines.append(f"`{task.short_id()}` {task.phase.value} {pct} {task.name or task.source.type.value}")
    return "\n".join(lines)


@app.on_message(filters.command("stats") & owner_filter)
async def stats(_, message: Message):
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
        await message.reply("Cancel requested.")
    else:
        await message.reply("Task not found.")


@app.on_message(filters.command("cancelall") & owner_filter)
async def cancel_all(_, message: Message):
    for task in manager.active_tasks():
        task.cancelled = True
    await message.reply("Cancel requested for all active tasks.")


@app.on_message(filters.command("delete") & owner_filter)
async def delete_cmd(_, message: Message):
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
    rmtree(target, ignore_errors=True)
    await query.message.edit(f"Deleted `{target}`")


@app.on_message(filters.command("ping") & owner_filter)
async def ping(_, message: Message):
    await message.reply("pong")


@app.on_message(filters.command("log") & owner_filter)
async def log_cmd(_, message: Message):
    log_path = Path("bot.log")
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
