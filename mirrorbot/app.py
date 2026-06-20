import asyncio
import logging
import secrets
import signal
from collections import defaultdict
from html import escape
from pathlib import Path
from time import time

from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .core.config import Config
from .core.logging_config import setup_logging
from .core.models import AddOptions, Destination, Source, SourceType, TaskPhase
from .core.parser import parse_add_text
from .core.source_detector import detect_source
from .context import BotContext
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
from .services.media_library import promote_yearless_series_folders
from .services.background import BackgroundTasks
from .services.runtime import RuntimeCoordinator
from .services.restart_state import take_restart_state
from .services.startup import cleanup_abandoned_downloads, prepare_local_library
from .services.web_dashboard import WebDashboard
from .telegram import keyboards as telegram_keyboards
from .telegram import messages as telegram_messages

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
    public_base_url(8005, config.public_base_url),
    8005,
    300,
)
status_messages: dict[int, Message] = {}
status_jobs: dict[int, asyncio.Task] = {}
pending_local_metadata_tasks: dict[str, object] = {}
pending_jellyfin_scan_reasons: set[str] = set()
local_metadata_job: asyncio.Task | None = None
last_jellyfin_auto_scan_at = 0.0
status_text: dict[int, str] = {}
status_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
series_promotion_job: asyncio.Task | None = None
PENDING_ADD_TIMEOUT = 120
JELLYFIN_AUTO_SCAN_COOLDOWN = 180
ADD_USAGE = telegram_messages.ADD_USAGE
HELP_TEXT = telegram_messages.HELP_TEXT

app = (
    Client(
        "mirrorbot",
        api_id=config.telegram_api_id,
        api_hash=config.telegram_api_hash,
        bot_token=config.bot_token,
        max_concurrent_transmissions=config.task_limit,
    )
    if config.enable_telegram_ui and config.bot_token
    else None
)
context = BotContext(
    config=config,
    manager=manager,
    background=background,
    jellyfin=jellyfin,
    jellyfin_api=jellyfin_api,
    drive_search_pages=drive_search_pages,
    drive_share_pages=drive_share_pages,
    telegram_app=app,
    get_file_explorer=lambda: get_file_explorer(),
)
web_dashboard: WebDashboard | None = None

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
    return telegram_keyboards.destination_buttons(token)

def local_buttons(token: str) -> InlineKeyboardMarkup:
    return telegram_keyboards.local_buttons(token)

def ytdlp_buttons(token: str) -> InlineKeyboardMarkup:
    return telegram_keyboards.ytdlp_buttons(token)

def ytdlp_video_buttons(token: str) -> InlineKeyboardMarkup:
    return telegram_keyboards.ytdlp_video_buttons(token)

def ytdlp_audio_buttons(token: str) -> InlineKeyboardMarkup:
    return telegram_keyboards.ytdlp_audio_buttons(token)

def result_list(title: str, items: list[str], links: list[str] | None = None) -> str:
    return telegram_messages.result_list(title, items, links)

def completion_message(task) -> str:
    return telegram_messages.completion_message(task)

def completion_buttons(task) -> InlineKeyboardMarkup | None:
    return telegram_keyboards.completion_buttons(task, jellyfin_url())

def completion_payload(task) -> dict:
    return telegram_messages.completion_payload(task, jellyfin_url())

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
        if task.destination in {Destination.LOCAL_MOVIES, Destination.LOCAL_SERIES}:
            schedule_local_metadata_refresh(task)
        if (
            task.phase == TaskPhase.COMPLETE
            and task.destination == Destination.LOCAL_SERIES
            and not task.library_name.endswith(")")
        ):
            schedule_series_promotion()
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
                reply_markup=completion_buttons(task),
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
    return public_base_url(8003, config.public_base_url)

def jellyfin_buttons() -> InlineKeyboardMarkup:
    return telegram_keyboards.jellyfin_buttons(jellyfin_url())

def format_jellyfin_status(status, action: str = "Status", server_info: dict | None = None) -> str:
    return telegram_messages.format_jellyfin_status(status, jellyfin_url(), action, server_info)

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

async def refresh_pending_local_metadata() -> None:
    global last_jellyfin_auto_scan_at, local_metadata_job
    try:
        while True:
            while any(
                task.destination in {Destination.LOCAL_MOVIES, Destination.LOCAL_SERIES}
                and not task.terminal
                for task in manager.tasks.values()
            ):
                await asyncio.sleep(5)
            await asyncio.sleep(2)
            if any(
                task.destination in {Destination.LOCAL_MOVIES, Destination.LOCAL_SERIES}
                and not task.terminal
                for task in manager.tasks.values()
            ):
                continue
            completed_count = len(pending_local_metadata_tasks)
            reasons = sorted(pending_jellyfin_scan_reasons)
            pending_local_metadata_tasks.clear()
            pending_jellyfin_scan_reasons.clear()
            if not completed_count and not reasons:
                return
            cooldown_wait = max(
                0,
                last_jellyfin_auto_scan_at + JELLYFIN_AUTO_SCAN_COOLDOWN - time(),
            )
            if cooldown_wait:
                LOGGER.info(
                    "Jellyfin auto scan delayed cooldown=%ss reasons=%s",
                    int(cooldown_wait),
                    ",".join(reasons) or "local-complete",
                )
                await asyncio.sleep(cooldown_wait)
                if any(
                    task.destination in {Destination.LOCAL_MOVIES, Destination.LOCAL_SERIES}
                    and not task.terminal
                    for task in manager.tasks.values()
                ):
                    continue
            try:
                await asyncio.to_thread(jellyfin_api.scan_library)
                last_jellyfin_auto_scan_at = time()
            except Exception:
                LOGGER.exception(
                    "Jellyfin scan and metadata refresh for completed local task batch failed"
                )
                return
            if not pending_local_metadata_tasks:
                return
    finally:
        local_metadata_job = None

def schedule_local_metadata_refresh(task) -> None:
    if task.phase == TaskPhase.COMPLETE:
        pending_local_metadata_tasks[task.id] = task
        pending_jellyfin_scan_reasons.add("local-complete")
    schedule_jellyfin_auto_scan()

def schedule_jellyfin_auto_scan() -> None:
    global local_metadata_job
    if any(
        active.destination in {Destination.LOCAL_MOVIES, Destination.LOCAL_SERIES}
        and not active.terminal
        for active in manager.tasks.values()
    ):
        return
    if (pending_local_metadata_tasks or pending_jellyfin_scan_reasons) and (
        local_metadata_job is None or local_metadata_job.done()
    ):
        local_metadata_job = background.create(
            refresh_pending_local_metadata(),
            name="jellyfin-local-metadata-batch",
        )

async def promote_series_library() -> None:
    promoted = 0
    for attempt in range(3):
        promotion = await asyncio.to_thread(
            promote_yearless_series_folders,
            config.local_download_root,
            config.tmdb_api_key,
        )
        promoted += promotion["promoted"]
        LOGGER.info(
            "Series folder promotion pass=%s promoted=%s skipped=%s conflicts=%s",
            attempt + 1,
            promotion["promoted"],
            promotion["skipped"],
            promotion["conflicts"],
        )
        if promotion["skipped"] == 0:
            break
        await asyncio.sleep(60)
    if promoted:
        pending_jellyfin_scan_reasons.add("series-promotion")
        schedule_jellyfin_auto_scan()

def schedule_series_promotion() -> None:
    global series_promotion_job
    if series_promotion_job is None or series_promotion_job.done():
        series_promotion_job = background.create(
            promote_series_library(),
            name="promote-series-library",
        )

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
                await app.send_message(
                    chat_id,
                    completion_message(upload_task),
                    parse_mode=ParseMode.HTML,
                    reply_markup=completion_buttons(upload_task),
                    disable_web_page_preview=True,
                )
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
            public_base_url(8004, config.public_base_url),
            explorer_upload,
            explorer_scan,
            8004,
        )
    return file_explorer

def register_command_handlers() -> None:
    """Import focused handler modules after shared app state is initialized."""
    if app is None:
        LOGGER.info("Telegram UI disabled; command handlers were not registered")
        return
    from .commands import add, common, drive, jellyfin, local  # noqa: F401

register_command_handlers()

async def close_file_explorer() -> None:
    if file_explorer is not None:
        await file_explorer.close_all()

async def close_web_dashboard() -> None:
    if web_dashboard is not None:
        await web_dashboard.close()

def telegram_client():
    return app

async def shutdown_bot() -> None:
    global shutting_down
    if shutting_down:
        return
    shutting_down = True
    LOGGER.info("Graceful shutdown started")
    for job in list(pending_add_expiry_jobs.values()) + list(pending_drive_delete_expiry_jobs.values()) + list(status_jobs.values()):
        job.cancel()
    await runtime.shutdown((drive_search_pages.close_all, drive_share_pages.close_all, close_file_explorer, close_web_dashboard))
    LOGGER.info("Graceful shutdown complete")

async def wait_for_shutdown_signal() -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    await stop_event.wait()

async def main() -> None:
    global web_dashboard
    LOGGER.info("========== BOT STARTED ================")
    await asyncio.to_thread(ensure_jellyfin_running)
    cleanup_abandoned_downloads(config.download_dir, config.local_download_root)
    prepare_local_library(config.local_download_root)

    web_dashboard = WebDashboard(
        config,
        manager,
        background,
        telegram_client,
        jellyfin,
        jellyfin_api,
        drive_search_pages,
        drive_share_pages,
        get_file_explorer,
        schedule_local_metadata_refresh,
        schedule_series_promotion,
        completion_payload,
    )
    await web_dashboard.start()

    telegram_started = False
    if app is not None:
        try:
            LOGGER.info("Starting Telegram UI")
            await app.start()
            telegram_started = True
        except Exception:
            LOGGER.exception("Telegram UI failed to start; web dashboard remains available")

    schedule_series_promotion()
    restart_state = await asyncio.to_thread(take_restart_state)
    if restart_state is not None and telegram_started:
        elapsed = max(0, round(time() - restart_state.requested_at))
        try:
            await app.edit_message_text(
                restart_state.chat_id,
                restart_state.message_id,
                f"Mirror-Bot restarted successfully in {elapsed}s.",
            )
            LOGGER.info("Restart success notification sent elapsed=%ss", elapsed)
        except Exception:
            LOGGER.exception("Could not send restart success notification")
    try:
        if telegram_started:
            await idle()
        else:
            await wait_for_shutdown_signal()
    finally:
        await shutdown_bot()
        if telegram_started:
            await app.stop()

def run():
    if app is not None:
        app.loop.run_until_complete(main())
    else:
        asyncio.run(main())
