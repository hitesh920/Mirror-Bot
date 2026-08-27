import asyncio
from pathlib import Path

from pyrogram import Client
from pyrogram.types import Message

from ..core.models import Task
from ..resolvers.base import safe_name
from ..services.transfer_guard import ensure_disk_space


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

    task.begin_progress()
    checked_total = 0

    async def progress(current: int, total: int):
        nonlocal checked_total
        if total and total != checked_total:
            ensure_disk_space(target, total)
            checked_total = total
        if task.cancelled:
            if client is None:
                raise asyncio.CancelledError()
            client.stop_transmission()
        task.report_progress(current, size=total)

    path = await message.download(file_name=str(target), progress=progress)
    if not path:
        raise asyncio.CancelledError()
    task.report_progress(task.downloaded, complete=True)
    return Path(path)
