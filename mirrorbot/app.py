import asyncio
import logging
import secrets
from collections import defaultdict
from html import escape
from pathlib import Path
from shutil import rmtree

from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .core.config import Config
from .core.logging_config import setup_logging
from .core.models import AddOptions, Destination, Source, SourceType, TaskPhase
from .core.parser import parse_add_text
from .core.source_detector import detect_source
from .downloaders.gdrive import drive_id_from_url
from .services.status import format_status
from .services.task_manager import TaskManager
from .services.google_drive_delivery import (
    delete_drive_item,
    drive_item_info,
)
from .services.drive_search_pages import DriveSearchPages
from .services.drive_share_pages import DriveSharePages
from .services.public_url import public_base_url
from .services.jellyfin import JellyfinControlError, JellyfinManager
from .services.jellyfin_api import JellyfinApi
from .services.file_explorer import FileExplorer
from .services.media_library import apply_media_permissions
from .services.background import BackgroundTasks
from .services.runtime import RuntimeCoordinator

setup_logging()
LOGGER = logging.getLogger(__name__)
config = Config.load()
manager = TaskManager(config)
jellyfin = JellyfinManager("jellyfin")
jellyfin_api = JellyfinApi(config.jellyfin_api_key)
file_explorer = None
background = BackgroundTasks()
runtime = RuntimeCoordinator(manager, background)
shutting_down = False
pending_adds: dict[str, tuple[Source, object, Message | None]] = {}
pending_add_messages: dict[str, Message] = {}
pending_add_expiry_jobs: dict[str, asyncio.Task] = {}
pending_drive_delete_chats: set[int] = set()
pending_drive_delete_items: dict[str, dict] = {}
pending_drive_delete_expiry_jobs: dict[str, asyncio.Task] = {}
drive_search_pages = DriveSearchPages(
    public_base_url(config.torrent_selection_port + 1, config.public_base_url),
    config.torrent_selection_port + 1,
    300,
)
drive_share_pages = DriveSharePages(
    public_base_url(8004, config.public_base_url),
    8004,
    300,
)
status_messages: dict[int, Message] = {}
status_jobs: dict[int, asyncio.Task] = {}
status_text: dict[int, str] = {}
status_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
PENDING_ADD_TIMEOUT = 120
ADD_USAGE = (
    "Usage: <code>/add &lt;link&gt; [-z|-zp password|-e|-ep password|-n name]</code>\n"
    "You can also reply to a Telegram file or link with <code>/add</code>."
)
HELP_TEXT = "\n".join(
    [
        "<b>Mirror-Bot commands</b>",
        "",
        "<b>Add</b>",
        "<code>/add &lt;link&gt;</code> - add a link",
        "<code>/add</code> - use the replied file/link",
        "<code>-z</code> zip, <code>-zp pass</code> password zip",
        "<code>-e</code> extract, <code>-ep pass</code> password extract",
        "<code>-n name</code> custom task name",
        "",
        "<b>Status</b>",
        "<code>/status</code> - live task status",
        "<code>/stats</code> - bot/server stats",
        "<code>/gdstats</code> - Google Drive auth and quota",
        "<code>/jellyfin</code> - manage Jellyfin",
        "<code>/local</code> - temporary local file explorer",
        "",
        "<b>Manage</b>",
        "<code>/cancel &lt;task-id&gt;</code> - cancel one task",
        "<code>/cancelall</code> - cancel all active tasks",
        "<code>/restart</code> - gracefully restart Mirror-Bot",
        "<code>/logs</code> - send recent sanitized application logs",
        "<code>/delete</code> - delete Local or Google Drive items",
        "<code>/delete &lt;drive-link-or-id&gt;</code> - delete Google Drive item",
        "",
        "<b>Google Drive</b>",
        "<code>/search &lt;name&gt;</code> - search Drive on a temporary page",
        "<code>/share &lt;drive-link&gt;</code> - temporary public Drive share page",
    ]
)

app = Client(
    "mirrorbot",
    api_id=config.telegram_api_id,
    api_hash=config.telegram_api_hash,
    bot_token=config.bot_token,
    max_concurrent_transmissions=config.task_limit,
)


def owner_only(_, __, message: Message) -> bool:
    user = message.from_user or message.sender_chat
    return bool(not shutting_down and user and user.id == config.owner_id)


owner_filter = filters.create(owner_only)


def chat_tasks(chat_id: int):
    return [
        task
        for task in manager.active_tasks()
        if task.chat_id == chat_id and task.status_visible
    ]


async def expire_pending_add(token: str) -> None:
    try:
        await asyncio.sleep(PENDING_ADD_TIMEOUT)
        pending = pending_adds.pop(token, None)
        message = pending_add_messages.pop(token, None)
        if pending is None:
            return
        LOGGER.info("Expired pending /add selection message_id=%s", token)
        if message:
            try:
                await message.edit("Selection expired. Send /add again.")
            except Exception:
                LOGGER.debug(
                    "Could not edit expired /add selection message_id=%s",
                    token,
                    exc_info=True,
                )
    finally:
        pending_add_expiry_jobs.pop(token, None)


def start_pending_add_expiry(token: str, message: Message) -> None:
    pending_add_messages[token] = message
    old_job = pending_add_expiry_jobs.pop(token, None)
    if old_job:
        old_job.cancel()
    pending_add_expiry_jobs[token] = background.create(expire_pending_add(token), name="expire-add")


def take_pending_add(token: str):
    pending = pending_adds.pop(token, None)
    pending_add_messages.pop(token, None)
    job = pending_add_expiry_jobs.pop(token, None)
    if job:
        job.cancel()
    return pending


async def answer_expired_selection(query) -> None:
    await query.answer("Expired task", show_alert=True)
    try:
        await query.message.edit("Selection expired. Send /add again.")
    except Exception:
        pass


async def expire_drive_delete(token: str, message: Message) -> None:
    try:
        await asyncio.sleep(PENDING_ADD_TIMEOUT)
        item = pending_drive_delete_items.pop(token, None)
        if item is None:
            return
        LOGGER.info("Expired Google Drive delete confirmation id=%s", item.get("id"))
        try:
            await message.edit("Google Drive delete request expired.")
        except Exception:
            LOGGER.debug(
                "Could not edit expired Google Drive delete confirmation",
                exc_info=True,
            )
    finally:
        pending_drive_delete_expiry_jobs.pop(token, None)


def start_drive_delete_expiry(token: str, message: Message) -> None:
    old_job = pending_drive_delete_expiry_jobs.pop(token, None)
    if old_job:
        old_job.cancel()
    pending_drive_delete_expiry_jobs[token] = background.create(expire_drive_delete(token, message), name="expire-drive-delete")


def take_pending_drive_delete(token: str) -> dict | None:
    item = pending_drive_delete_items.pop(token, None)
    job = pending_drive_delete_expiry_jobs.pop(token, None)
    if job:
        job.cancel()
    return item


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
        status_jobs[chat_id] = background.create(status_loop(chat_id), name="status-loop")


async def send_live_status(chat_id: int) -> None:
    await update_status_message(chat_id)
    job = status_jobs.get(chat_id)
    if job is None or job.done():
        status_jobs[chat_id] = background.create(status_loop(chat_id), name="status-loop")


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
            [
                InlineKeyboardButton("Video", callback_data=f"ytkind:video:{token}"),
                InlineKeyboardButton("Audio", callback_data=f"ytkind:audio:{token}"),
            ],
        ]
    )


def ytdlp_video_buttons(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("360p", callback_data=f"yt:video:360:{token}"),
                InlineKeyboardButton("480p", callback_data=f"yt:video:480:{token}"),
            ],
            [
                InlineKeyboardButton("720p", callback_data=f"yt:video:720:{token}"),
                InlineKeyboardButton("1080p", callback_data=f"yt:video:1080:{token}"),
            ],
            [InlineKeyboardButton("Back", callback_data=f"ytkind:back:{token}")],
        ]
    )


def ytdlp_audio_buttons(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("64 kbps", callback_data=f"yt:audio:64:{token}"),
                InlineKeyboardButton("128 kbps", callback_data=f"yt:audio:128:{token}"),
            ],
            [
                InlineKeyboardButton("192 kbps", callback_data=f"yt:audio:192:{token}"),
                InlineKeyboardButton("256 kbps", callback_data=f"yt:audio:256:{token}"),
            ],
            [InlineKeyboardButton("320 kbps", callback_data=f"yt:audio:320:{token}")],
            [InlineKeyboardButton("Back", callback_data=f"ytkind:back:{token}")],
        ]
    )


def result_list(title: str, items: list[str], links: list[str] | None = None) -> str:
    if not items:
        return ""
    limit = 20
    lines = [f"<b>{escape(title)}:</b>"]
    for index, name in enumerate(items[:limit]):
        safe_name = escape(name[:120])
        if links and index < len(links) and links[index]:
            lines.append(f'<a href="{escape(links[index], quote=True)}">Open</a> - <code>{safe_name}</code>')
        else:
            lines.append(f"<code>{safe_name}</code>")
    if len(items) > limit:
        lines.append(f"<i>...and {len(items) - limit} more</i>")
    return "\n".join(lines)


def completion_message(task) -> str:
    name = escape(task.name or task.result_name or task.source.type.value)
    if task.destination == Destination.TELEGRAM:
        sections = [
            "<b>Task complete</b>",
            f"<b>Name:</b> <code>{name}</code>",
            "<b>Uploaded to:</b> <code>Telegram</code>",
            f"<b>Files:</b> <code>{len(task.result_files)}</code>",
            result_list("Uploaded files", task.result_files, task.result_links),
        ]
    elif task.destination == Destination.GOOGLE_DRIVE:
        drive_link = (
            f'<b>Drive link:</b> <a href="{escape(task.result_links[0], quote=True)}">Open</a>'
            if task.result_links
            else ""
        )
        sections = [
            "<b>Task complete</b>",
            f"<b>Name:</b> <code>{name}</code>",
            "<b>Uploaded to:</b> <code>Google Drive</code>",
            f"<b>Files:</b> <code>{len(task.result_files)}</code>",
            f"<b>Folders:</b> <code>{len(task.result_folders)}</code>",
            drive_link,
        ]
    else:
        local_name = escape(task.library_name or task.result_name or task.name or task.source.type.value)
        sections = [
            "<b>Task complete</b>",
            f"<b>Name:</b> <code>{local_name}</code>",
            f"<b>Uploaded to:</b> <code>{escape(str(task.result_path or 'Local'))}</code>",
            f"<b>Files:</b> <code>{len(task.result_files)}</code>",
            f"<b>Folders:</b> <code>{len(task.result_folders)}</code>",
            result_list("Files", task.result_files),
            result_list("Folders", task.result_folders),
            f'<b>Jellyfin:</b> <a href="{escape(jellyfin_url(), quote=True)}">Open</a>',
        ]
    return "\n".join(section for section in sections if section)














async def launch_selected_task(query, token: str, destination: Destination) -> None:
    pending = take_pending_add(token)
    if pending is None:
        await answer_expired_selection(query)
        return
    source, options, reply = pending
    task = manager.create_task(
        query.from_user.id,
        query.message.chat.id,
        int(token),
        source,
        destination,
        options,
    )
    LOGGER.info("Task %s: selected destination=%s", task.short_id(), destination.value)
    is_torrent = source.type in {SourceType.MAGNET, SourceType.TORRENT_FILE}
    if is_torrent:
        task.status_visible = False
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
                    [
                        [
                            InlineKeyboardButton(
                                "Click here to review files",
                                url=selected_task.selection_url,
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "Cancel",
                                callback_data=f"selcancel:{selected_task.short_id()}",
                            )
                        ],
                    ]
                ),
                disable_web_page_preview=True,
            )

        async def selector_done(selector_message):
            try:
                await selector_message.delete()
            except Exception:
                pass
            if task.phase == TaskPhase.DOWNLOADING:
                task.status_visible = True
                await replace_status_message(task.chat_id)
                job = status_jobs.get(task.chat_id)
                if job is None or job.done():
                    status_jobs[task.chat_id] = background.create(
                        status_loop(task.chat_id), name="status-loop"
                    )

        await manager.run_task(
            task,
            telegram_reply=reply,
            telegram_client=app,
            on_selector_ready=selector_ready,
            on_selector_done=selector_done,
        )
        if task.phase == TaskPhase.COMPLETE and task.destination in {Destination.LOCAL_MOVIES, Destination.LOCAL_SERIES}:
            try:
                await asyncio.to_thread(jellyfin_api.scan_library)
            except Exception:
                LOGGER.exception("Task %s: Jellyfin scan request failed", task.short_id())
        if is_torrent:
            try:
                await query.message.delete()
            except Exception:
                pass
        if task.phase.value == "complete":
            await app.send_message(
                task.chat_id,
                completion_message(task),
                parse_mode=ParseMode.HTML,
                reply_to_message_id=task.message_id,
                disable_web_page_preview=True,
            )
        elif task.error:
            await app.send_message(
                task.chat_id,
                f"Task {task.short_id()} failed:\n{task.error}",
                parse_mode=ParseMode.DISABLED,
                reply_to_message_id=task.message_id,
            )
        else:
            await app.send_message(
                task.chat_id,
                f"Task {task.short_id()} {task.phase.value}.",
                parse_mode=ParseMode.DISABLED,
                reply_to_message_id=task.message_id,
            )
        await update_status_message(task.chat_id)

    manager.spawn(runner(), name="transfer-task")
    if not is_torrent:
        try:
            await query.message.delete()
        except Exception:
            pass
    if not is_torrent:
        await asyncio.sleep(0)
        await replace_status_message(task.chat_id)
        job = status_jobs.get(task.chat_id)
        if job is None or job.done():
            status_jobs[task.chat_id] = background.create(status_loop(task.chat_id), name="status-loop")






















async def delete_google_drive_link(message: Message, link: str) -> None:
    try:
        file_id = drive_id_from_url(link)
    except ValueError as exc:
        await message.reply(str(exc))
        return
    try:
        item = await asyncio.to_thread(drive_item_info, config, file_id)
    except Exception as exc:
        LOGGER.exception("Google Drive item lookup failed")
        await message.reply(
            f"Google Drive item lookup failed:\n{exc}",
            parse_mode=ParseMode.DISABLED,
        )
        return
    token = secrets.token_urlsafe(16)
    pending_drive_delete_items[token] = item
    item_type = "folder" if item.get("mimeType") == "application/vnd.google-apps.folder" else "file"
    prompt = await message.reply(
        "<b>Confirm Google Drive delete</b>\n"
        f"<b>Name:</b> <code>{escape(item.get('name', 'Untitled'))}</code>\n"
        f"<b>Type:</b> <code>{item_type}</code>\n"
        f"<b>ID:</b> <code>{escape(file_id)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Delete", callback_data=f"dgddel:{token}"),
                    InlineKeyboardButton("Cancel", callback_data=f"dgdcancel:{token}"),
                ]
            ]
        ),
    )
    start_drive_delete_expiry(token, prompt)




def jellyfin_url() -> str:
    return public_base_url(8002, config.public_base_url)


def jellyfin_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Open Jellyfin", url=jellyfin_url())],
            [
                InlineKeyboardButton("Start", callback_data="jf:start"),
                InlineKeyboardButton("Stop", callback_data="jf:stop"),
            ],
            [
                InlineKeyboardButton("Restart", callback_data="jf:restart"),
                InlineKeyboardButton("Refresh", callback_data="jf:refresh"),
            ],
            [
                InlineKeyboardButton("Scan Library", callback_data="jf:scan"),
            ],
        ]
    )


def format_jellyfin_status(status, action: str = "Status", server_info: dict | None = None) -> str:
    running = "yes" if status.running else "no"
    lines = [
        "<b>Jellyfin</b>",
        f"<b>Action:</b> <code>{escape(action)}</code>",
        f"<b>Container:</b> <code>{escape(status.name)}</code>",
        f"<b>State:</b> <code>{escape(status.state)}</code>",
        f"<b>Health:</b> <code>{escape(status.health)}</code>",
        f"<b>Running:</b> <code>{running}</code>",
    ]
    if server_info:
        lines.extend([
            f"<b>Server:</b> <code>{escape(server_info.get('ServerName', 'unknown'))}</code>",
            f"<b>Version:</b> <code>{escape(server_info.get('Version', 'unknown'))}</code>",
        ])
    lines.append(f"<b>URL:</b> <code>{escape(jellyfin_url())}</code>")
    return "\n".join(lines)


async def jellyfin_server_info(status) -> dict:
    if not status.running:
        return {}
    try:
        return await asyncio.to_thread(jellyfin_api.system_info)
    except Exception as exc:
        LOGGER.warning("Jellyfin server information failed error=%s", type(exc).__name__)
        return {}


async def jellyfin_status_text(action: str = "Status") -> str:
    status = await asyncio.to_thread(jellyfin.status)
    return format_jellyfin_status(status, action, await jellyfin_server_info(status))






def ensure_jellyfin_running() -> None:
    try:
        status = jellyfin.ensure_running()
        LOGGER.info(
            "Jellyfin ensure running: container=%s state=%s health=%s",
            status.name,
            status.state,
            status.health,
        )
    except JellyfinControlError:
        LOGGER.exception("Jellyfin ensure running failed")
    except Exception:
        LOGGER.exception("Unexpected Jellyfin startup check failure")


async def explorer_scan() -> None:
    await asyncio.to_thread(jellyfin_api.scan_library)


async def explorer_upload(
    chat_id: int,
    paths: list[Path],
    destination_name: str,
) -> None:
    destination = Destination(destination_name)
    for path in paths:
        task = manager.create_task(
            config.owner_id,
            chat_id,
            0,
            Source(SourceType.LOCAL_PATH, str(path), path.name),
            destination,
            AddOptions(),
        )

        async def runner(upload_task=task, upload_path=path):
            await manager.run_local_upload(upload_task, upload_path, app)
            if upload_task.phase == TaskPhase.COMPLETE:
                await app.send_message(chat_id, completion_message(upload_task), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            elif upload_task.error:
                await app.send_message(chat_id, f"Task {upload_task.short_id()} failed:\n{upload_task.error}", parse_mode=ParseMode.DISABLED)
            await update_status_message(chat_id)

        manager.spawn(runner(), name="transfer-task")
    await send_live_status(chat_id)


def get_file_explorer() -> FileExplorer:
    global file_explorer
    if file_explorer is None:
        file_explorer = FileExplorer(
            config.local_download_root,
            public_base_url(8003, config.public_base_url),
            explorer_upload,
            explorer_scan,
        )
    return file_explorer



def register_command_handlers() -> None:
    """Import focused handler modules after shared app state is initialized."""
    from .commands import add, common, drive, jellyfin, local  # noqa: F401


register_command_handlers()


async def close_file_explorer() -> None:
    if file_explorer is not None:
        await file_explorer.close_all()


async def shutdown_bot() -> None:
    global shutting_down
    if shutting_down:
        return
    shutting_down = True
    LOGGER.info("Graceful shutdown started")
    for job in list(pending_add_expiry_jobs.values()) + list(pending_drive_delete_expiry_jobs.values()) + list(status_jobs.values()):
        job.cancel()
    await runtime.shutdown((drive_search_pages.close_all, drive_share_pages.close_all, close_file_explorer))
    LOGGER.info("Graceful shutdown complete")


async def main() -> None:
    LOGGER.info("========== BOT STARTED ================")
    await asyncio.to_thread(ensure_jellyfin_running)
    cleanup_abandoned_downloads()
    (config.local_download_root / "movies").mkdir(parents=True, exist_ok=True)
    (config.local_download_root / "series").mkdir(parents=True, exist_ok=True)
    apply_media_permissions(config.local_download_root, config.local_download_root / "movies")
    apply_media_permissions(config.local_download_root, config.local_download_root / "series")
    LOGGER.info("Starting bot")
    await app.start()
    try:
        await idle()
    finally:
        await shutdown_bot()
        await app.stop()


def run():
    app.loop.run_until_complete(main())


def cleanup_abandoned_downloads() -> None:
    root = config.download_dir.resolve()
    local_root = config.local_download_root.resolve()
    if root == Path(root.anchor) or root == local_root or root in local_root.parents:
        raise RuntimeError(f"Unsafe temporary download directory: {root}")

    root.mkdir(parents=True, exist_ok=True)
    removed = 0
    for item in root.iterdir():
        if item.is_symlink() or item.is_file():
            item.unlink(missing_ok=True)
        elif item.is_dir():
            rmtree(item)
        removed += 1
    if removed:
        LOGGER.info("Removed %s abandoned download workspace(s)", removed)
