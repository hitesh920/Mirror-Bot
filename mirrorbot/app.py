import asyncio
import logging
import signal
from contextlib import suppress
from dataclasses import dataclass, replace
from time import time

from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .core.config import Config
from .core.logging_config import setup_logging
from .core.models import AddOptions, Destination, Source, SourceType, TaskPhase
from .services.background import BackgroundTasks
from .services.r2_delivery import expiry_sweeper
from .services.restart_state import take_restart_state
from .services.runtime import RuntimeCoordinator
from .services.startup import cleanup_abandoned_downloads
from .services.task_manager import TaskManager
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


@dataclass
class PendingAdd:
    sources: list[Source]
    options: AddOptions
    reply: Message | None = None
    batch_mode: str = ""
    skipped: int = 0


pending_adds: dict[str, PendingAdd] = {}
pending_add_messages: dict[str, Message] = {}
pending_add_expiry_jobs: dict[str, asyncio.Task] = {}
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
    pending_add_expiry_jobs[token] = background.create(
        expire_pending_add(token), name="expire-add"
    )


def take_pending_add(token: str):
    pending = pending_adds.pop(token, None)
    pending_add_messages.pop(token, None)
    job = pending_add_expiry_jobs.pop(token, None)
    if job:
        job.cancel()
    return pending


async def answer_expired_selection(query) -> None:
    await query.answer("Expired task", show_alert=True)
    with suppress(Exception):
        await query.message.edit("Selection expired. Send /add again.")


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


def batch_mode_buttons(token: str) -> InlineKeyboardMarkup:
    return telegram_keyboards.batch_mode_buttons(token)


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


def _spawn_transfer_task(task, reply, query, *, is_torrent: bool = False) -> None:
    LOGGER.info(
        "Task %s: selected destination=%s",
        task.short_id(),
        task.destination.value,
    )

    async def runner():
        async def selector_ready(selected_task):
            with suppress(Exception):
                await query.message.delete()
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
            with suppress(Exception):
                await selector_message.delete()
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
            with suppress(Exception):
                await query.message.delete()
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


async def launch_selected_task(
    query,
    token: str,
    destination: Destination,
) -> None:
    pending = take_pending_add(token)
    if pending is None:
        await answer_expired_selection(query)
        return
    if pending.options.batch_messages:
        await _launch_batch_tasks(query, token, destination, pending)
        return

    source = pending.sources[0]
    task = manager.create_task(
        query.from_user.id,
        query.message.chat.id,
        int(token),
        source,
        destination,
        pending.options,
    )
    is_torrent = source.type in {SourceType.MAGNET, SourceType.TORRENT_FILE}
    if is_torrent:
        task.status_visible = False
        await query.message.edit("Collecting torrent metadata...")
    _spawn_transfer_task(task, pending.reply, query, is_torrent=is_torrent)
    if not is_torrent:
        with suppress(Exception):
            await query.message.delete()
    if not is_torrent:
        await asyncio.sleep(0)
        await replace_status_message(task.chat_id)


async def _launch_batch_tasks(
    query,
    token: str,
    destination: Destination,
    pending: PendingAdd,
) -> None:
    if pending.batch_mode == "separate":
        tasks = []
        for source in pending.sources:
            options = replace(pending.options, name="", batch_messages=0)
            task = manager.create_task(
                query.from_user.id,
                query.message.chat.id,
                int(token),
                source,
                destination,
                options,
            )
            tasks.append(task)
            _spawn_transfer_task(task, None, query)
        await query.message.edit(
            f"{len(tasks)} tasks started. Each transfer will finish independently."
        )
        await asyncio.sleep(0)
        await replace_status_message(query.message.chat.id)
        return

    if pending.batch_mode != "zip":
        await query.message.edit("Batch mode was not selected. Send /add again.")
        return
    archive_stem = pending.options.name or f"batch-{token}"
    options = replace(
        pending.options,
        name=archive_stem,
        zip=True,
        zip_password="",
    )
    source = Source(
        SourceType.BATCH,
        "",
        archive_stem,
        {"sources": pending.sources},
    )
    task = manager.create_task(
        query.from_user.id,
        query.message.chat.id,
        int(token),
        source,
        destination,
        options,
    )
    task.batch_total = len(pending.sources)
    task.batch_initial_skipped = pending.skipped
    _spawn_transfer_task(task, None, query)
    with suppress(Exception):
        await query.message.delete()
    await asyncio.sleep(0)
    await replace_status_message(task.chat_id)


def register_command_handlers() -> None:
    """Import focused handler modules after shared app state is initialized."""
    if app is None:
        LOGGER.info("Telegram UI disabled; command handlers were not registered")
        return
    from .commands import add, common, r2  # noqa: F401


register_command_handlers()


async def shutdown_bot() -> None:
    global shutting_down
    if shutting_down:
        return
    shutting_down = True
    LOGGER.info("Graceful shutdown started")
    telegram_status.cancel_jobs()
    for job in list(pending_add_expiry_jobs.values()):
        job.cancel()
    await runtime.shutdown()
    LOGGER.info("Graceful shutdown complete")


async def wait_for_shutdown_signal() -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
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
            "Telegram dump channel is not reachable. Telegram uploads will "
            "fall back to the requester chat when possible.",
            exc_info=True,
        )


async def send_r2_expiry_warning(upload: dict) -> None:
    if app is None:
        raise RuntimeError("Telegram UI is unavailable")
    await app.send_message(
        config.owner_id,
        telegram_messages.r2_expiry_warning_message(upload),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        disable_notification=False,
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
    if config.r2_configured and config.r2_auto_delete_seconds > 0:
        warning_callback = send_r2_expiry_warning if telegram_started else None
        if warning_callback is None:
            LOGGER.warning(
                "R2 expiry deletion remains active, but Telegram deletion "
                "warnings are unavailable"
            )
        background.create(
            expiry_sweeper(config, warning_callback),
            name="r2-expiry-sweeper",
        )
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
