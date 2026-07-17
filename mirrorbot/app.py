import asyncio
import logging
import secrets
import signal
from html import escape
from time import time

from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .core.config import Config
from .core.logging_config import setup_logging
from .core.models import Destination, Source, SourceType, TaskPhase
from .downloaders.gdrive import drive_id_from_url
from .services.task_manager import TaskManager
from .services.google_drive_delivery import (
    DRIVE_CATEGORY_FOLDERS,
    drive_item_info,
    ensure_drive_category_folders,
)
from .services.drive_search_pages import DriveSearchPages
from .services.drive_share_pages import DriveSharePages
from .services.public_url import public_base_url
from .services.background import BackgroundTasks
from .services.runtime import RuntimeCoordinator
from .services.restart_state import take_restart_state
from .services.startup import cleanup_abandoned_downloads
from .services.telegram_delivery import telegram_chat_id
from .telegram import keyboards as telegram_keyboards
from .telegram import messages as telegram_messages
from .telegram.status import TelegramStatus

setup_logging()
LOGGER = logging.getLogger(__name__)
config = Config.load()
manager = TaskManager(config)
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
    public_base_url(8003, config.public_base_url),
    8003,
    300,
)
PENDING_ADD_TIMEOUT = 120
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
telegram_status = TelegramStatus(
    app,
    manager,
    background,
    config.status_update_interval,
)

def owner_only(_, __, message: Message) -> bool:
    user = message.from_user or message.sender_chat
    return bool(not shutting_down and user and user.id == config.owner_id)

owner_filter = filters.create(owner_only)


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
    await telegram_status.update(chat_id)

async def replace_status_message(chat_id: int) -> None:
    await telegram_status.replace(chat_id)

async def start_live_status(chat_id: int, message: Message) -> None:
    await telegram_status.start(chat_id, message)

async def send_live_status(chat_id: int) -> None:
    await telegram_status.send(chat_id)

def destination_buttons(token: str) -> InlineKeyboardMarkup:
    return telegram_keyboards.destination_buttons(token)

def google_drive_folder_buttons(token: str) -> InlineKeyboardMarkup:
    return telegram_keyboards.google_drive_folder_buttons(token)

def ytdlp_buttons(token: str) -> InlineKeyboardMarkup:
    return telegram_keyboards.ytdlp_buttons(token)

def ytdlp_video_buttons(token: str) -> InlineKeyboardMarkup:
    return telegram_keyboards.ytdlp_video_buttons(token)

def ytdlp_audio_buttons(token: str) -> InlineKeyboardMarkup:
    return telegram_keyboards.ytdlp_audio_buttons(token)

def completion_message(task) -> str:
    return telegram_messages.completion_message(task)

def completion_buttons(task) -> InlineKeyboardMarkup | None:
    return telegram_keyboards.completion_buttons(task)

async def launch_selected_task(
    query,
    token: str,
    destination: Destination,
    drive_folder_id: str = "",
    drive_folder_name: str = "",
) -> None:
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
    task.drive_folder_id = drive_folder_id
    task.drive_folder_name = drive_folder_name
    LOGGER.info(
        "Task %s: selected destination=%s drive_category=%r",
        task.short_id(),
        destination.value,
        drive_folder_name,
    )
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

def register_command_handlers() -> None:
    """Import focused handler modules after shared app state is initialized."""
    if app is None:
        LOGGER.info("Telegram UI disabled; command handlers were not registered")
        return
    from .commands import add, common, drive  # noqa: F401

register_command_handlers()

async def shutdown_bot() -> None:
    global shutting_down
    if shutting_down:
        return
    shutting_down = True
    LOGGER.info("Graceful shutdown started")
    telegram_status.cancel_jobs()
    for job in list(pending_add_expiry_jobs.values()) + list(pending_drive_delete_expiry_jobs.values()):
        job.cancel()
    await runtime.shutdown((drive_search_pages.close_all, drive_share_pages.close_all))
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


async def validate_telegram_dump_channel() -> None:
    upload_chat_id = telegram_chat_id(config.telegram_dump_chat_id)
    if app is None or upload_chat_id is None:
        return
    try:
        chat = await app.get_chat(upload_chat_id)
        LOGGER.info(
            "Telegram dump channel reachable id=%s title=%r",
            getattr(chat, "id", upload_chat_id),
            getattr(chat, "title", "") or getattr(chat, "username", ""),
        )
    except Exception:
        LOGGER.warning(
            "Telegram dump channel is not reachable. Telegram uploads will fall back to the requester chat when possible.",
            exc_info=True,
        )


async def prepare_google_drive_categories() -> None:
    try:
        folder_ids = await asyncio.to_thread(
            ensure_drive_category_folders,
            config,
        )
        LOGGER.info(
            "Google Drive categories ready folders=%s",
            ",".join(
                DRIVE_CATEGORY_FOLDERS[slug]
                for slug in folder_ids
            ),
        )
    except Exception:
        LOGGER.warning(
            "Google Drive category setup failed during startup; Drive uploads require a bot restart after the issue is fixed",
            exc_info=True,
        )


async def main() -> None:
    LOGGER.info("========== BOT STARTED ================")
    await manager.cleanup_orphaned_torrents()
    cleanup_abandoned_downloads(config.download_dir)

    telegram_started = False
    if app is not None:
        try:
            LOGGER.info("Starting Telegram UI")
            await app.start()
            telegram_started = True
            await validate_telegram_dump_channel()
            await prepare_google_drive_categories()
        except Exception:
            LOGGER.exception("Telegram UI failed to start")

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
