"""Core status, cancellation, health, and help handlers."""

import asyncio
import logging
import os
import signal
from html import escape
from pathlib import Path

import psutil
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from ..app import (
    HELP_TEXT, LOGGER, app, background, chat_tasks, config, manager, owner_filter,
    replace_status_message, status_jobs, status_loop,
)
from ..core.logging_config import create_log_export, log_event
from ..services.restart_state import save_restart_state
from ..services.speedtest import SpeedtestError, run_speedtest


speedtest_lock = asyncio.Lock()


@app.on_message(filters.command("start") & owner_filter)
async def start(_, message: Message):
    await message.reply("Bot is online. Use /help to see commands.")


@app.on_message(filters.command("help") & owner_filter)
async def help_cmd(_, message: Message):
    await message.reply(HELP_TEXT, parse_mode=ParseMode.HTML)


@app.on_message(filters.command("status") & owner_filter)
async def status(_, message: Message):
    log_event(LOGGER, logging.INFO, "command.status", result="requested")
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
        status_jobs[message.chat.id] = background.create(status_loop(message.chat.id), name="status-loop")


@app.on_message(filters.command("stats") & owner_filter)
async def stats(_, message: Message):
    log_event(LOGGER, logging.INFO, "command.stats", result="requested")
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
        await message.reply("Usage: <code>/cancel &lt;task-id&gt;</code>", parse_mode=ParseMode.HTML)
        return
    if manager.cancel(parts[1]):
        log_event(
            LOGGER, logging.INFO, "command.cancel", task=parts[1], result="requested"
        )
        await manager.close_active_selector(parts[1])
        await message.reply("Cancel requested.")
    else:
        await message.reply("Task not found or already finished.")


@app.on_message(filters.command("cancelall") & owner_filter)
async def cancel_all(_, message: Message):
    log_event(
        LOGGER,
        logging.INFO,
        "command.cancelall",
        result="requested",
        active_tasks=len(manager.active_tasks()),
    )
    for task in manager.active_tasks():
        manager.cancel(task.id)
    await manager.close_active_selector()
    await message.reply("Cancel requested for all active tasks.")


@app.on_callback_query(filters.regex(r"^selcancel:"))
async def cancel_selector(_, query):
    if query.from_user.id != config.owner_id:
        await query.answer("Not allowed", show_alert=True)
        return
    _, task_id = query.data.split(":", 1)
    if not manager.cancel(task_id):
        await query.answer("Task is no longer active", show_alert=True)
        return
    await query.answer("Cancel requested")
    await manager.close_active_selector(task_id)


@app.on_message(filters.command("ping") & owner_filter)
async def ping(_, message: Message):
    log_event(LOGGER, logging.INFO, "command.ping", result="requested")
    await message.reply("pong")


@app.on_message(filters.command("speedtest") & owner_filter)
async def speedtest_cmd(_, message: Message):
    if speedtest_lock.locked():
        await message.reply("A speed test is already running.")
        return
    log_event(LOGGER, logging.INFO, "command.speedtest", result="requested")
    status = await message.reply("Running speed test...")
    async with speedtest_lock:
        try:
            result = await run_speedtest()
        except SpeedtestError as exc:
            log_event(LOGGER, logging.WARNING, "command.speedtest", result="failed")
            await status.edit_text(
                f"<b>Speed test failed</b>\n<code>{escape(str(exc))}</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            LOGGER.exception("Unexpected speed test failure")
            await status.edit_text("Speed test failed unexpectedly.")
            return
    log_event(LOGGER, logging.INFO, "command.speedtest", result="completed")
    await status.edit_text(
        "\n".join(
            [
                "<b>Speed Test</b>",
                f"<b>Ping:</b> <code>{result.ping_ms:.2f} ms</code>",
                f"<b>Download:</b> <code>{result.download_mbps:.2f} Mbps</code>",
                f"<b>Upload:</b> <code>{result.upload_mbps:.2f} Mbps</code>",
                f"<b>ISP:</b> <code>{escape(result.isp)}</code>",
                f"<b>Server:</b> <code>{escape(result.sponsor)} - {escape(result.server)}</code>",
            ]
        ),
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("logs") & owner_filter)
async def logs_cmd(_, message: Message):
    log_event(LOGGER, logging.INFO, "command.logs", result="requested")
    exported = await asyncio.to_thread(create_log_export, config.log_file)
    if exported is None:
        await message.reply("No log file yet.")
        return
    try:
        await message.reply_document(
            str(exported),
            file_name="mirror-bot-logs.txt",
            caption="Latest 2,000 sanitized application log lines.",
        )
        log_event(LOGGER, logging.INFO, "command.logs", result="sent")
    finally:
        Path(exported).unlink(missing_ok=True)


@app.on_message(filters.command("restart") & owner_filter)
async def restart_cmd(_, message: Message):
    log_event(LOGGER, logging.INFO, "command.restart", result="requested")
    restart_message = await message.reply("Restarting Mirror-Bot...")
    await asyncio.to_thread(
        save_restart_state,
        restart_message.chat.id,
        restart_message.id,
    )
    LOGGER.info("========== RESTART REQUESTED ==========")
    await asyncio.sleep(0.5)
    os.kill(1, signal.SIGTERM)
