from pathlib import Path

from pyrogram.types import Message

from ..models import Task


async def download_telegram_file(task: Task, message: Message) -> Path:
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

    filename = task.options.name or getattr(media, "file_name", None) or f"telegram-{message.id}"
    target = task.work_dir / filename
    task.name = filename

    async def progress(current: int, total: int):
        task.downloaded = current
        task.size = total
        task.progress = current / total if total else 0

    path = await message.download(file_name=str(target), progress=progress)
    return Path(path)
