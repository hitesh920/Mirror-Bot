"""Entrypoint: wires the command handlers to the client and runs the loop."""

import asyncio
import logging
import signal
from contextlib import suppress
from time import time

from pyrogram import idle
from pyrogram.enums import ParseMode

from . import context
from .commands import add, common, r2  # noqa: F401  -- registers handlers on import
from .context import app, background, config, manager, pending_adds, telegram_status
from .services.r2_delivery import expiry_sweeper
from .services.restart_state import take_restart_state
from .services.runtime import RuntimeCoordinator
from .services.startup import cleanup_abandoned_downloads
from .services.telegram_delivery import telegram_chat_id
from .telegram import messages as telegram_messages

LOGGER = logging.getLogger(__name__)
runtime = RuntimeCoordinator(manager, background)


async def shutdown_bot() -> None:
    if context.is_shutting_down():
        return
    context.begin_shutdown()
    LOGGER.info("Graceful shutdown started")
    telegram_status.cancel_jobs()
    pending_adds.cancel_all()
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
    if upload_chat_id is None:
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
    await app.send_message(
        config.owner_id,
        telegram_messages.r2_expiry_warning_message(upload),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        disable_notification=False,
    )


async def main() -> None:
    LOGGER.info("========== BOT STARTED ================")
    await manager.resolve_public_base_url()
    await manager.cleanup_orphaned_torrents()
    cleanup_abandoned_downloads(config.download_dir)
    telegram_started = False
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
    app.loop.run_until_complete(main())
