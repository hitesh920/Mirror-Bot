import asyncio
from pathlib import Path
from time import monotonic

from pyrogram import Client
from pyrogram.types import Message

from ..core.models import Task
from ..resolvers.base import safe_name
from ..services.transfer_guard import reserve_disk_space, update_disk_reservation


async def download_telegram_file(
    task: Task, message: Message, client: Client | None = None
) -> Path:
    task.work_dir.mkdir(parents=True, exist_ok=True)
    media = (
        message.document
        or message.video
        or message.audio
        or message.photo
        or message.animation
        or message.voice
        or message.video_note
        or message.sticker
    )
    if media is None:
        raise ValueError("Reply does not contain a downloadable Telegram file")

    filename = safe_name(
        task.options.name
        or getattr(media, "file_name", "")
        or f"telegram-{message.id}",
        f"telegram-{message.id}",
    )
    target = task.work_dir / filename
    task.name = filename

    started = monotonic()
    checked_total = int(getattr(media, "file_size", 0) or 0)
    if checked_total:
        await reserve_disk_space(task, target, checked_total)
        task.size = checked_total

    async def progress(current: int, total: int):
        nonlocal checked_total
        if total and total != checked_total:
            await reserve_disk_space(task, target, max(0, total - current))
            checked_total = total
        if task.cancelled:
            if client is None:
                raise asyncio.CancelledError()
            client.stop_transmission()
        task.downloaded = current
        if total:
            await update_disk_reservation(task, max(0, total - current))
        task.size = total
        task.progress = current / total if total else 0
        elapsed = monotonic() - started
        task.speed = int(current / elapsed) if elapsed else 0
        task.eta = int((total - current) / task.speed) if task.speed else 0

    path = await message.download(file_name=str(target), progress=progress)
    if not path:
        raise asyncio.CancelledError()
    task.progress = 1
    task.eta = 0
    return Path(path)
