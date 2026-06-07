import asyncio
from hashlib import sha1
import logging
from pathlib import Path
from shutil import rmtree
from time import monotonic

import aiofiles
from pyrogram import Client
from pyrogram.enums import ParseMode

from ..core.models import Task, TaskPhase
from ..downloaders.process import path_size

LOGGER = logging.getLogger(__name__)
VIDEO_EXTENSIONS = {".mkv", ".m4v", ".mov", ".mp4", ".webm"}
AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
PHOTO_EXTENSIONS = {".jpeg", ".jpg", ".png", ".webp"}
ANIMATION_EXTENSIONS = {".gif"}


def upload_files(path: Path) -> list[tuple[Path, str]]:
    if path.is_file():
        return [(path, path.name)]
    return [
        (item, item.relative_to(path).as_posix())
        for item in sorted(path.rglob("*"))
        if item.is_file()
    ]


def telegram_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in PHOTO_EXTENSIONS:
        return "photo"
    if suffix in ANIMATION_EXTENSIONS:
        return "animation"
    return "document"


async def send_telegram_file(
    client: Client,
    task: Task,
    item: Path,
    caption: str,
    progress,
    media_type: str,
):
    common = {
        "chat_id": task.chat_id,
        "caption": caption,
        "parse_mode": ParseMode.DISABLED,
        "disable_notification": True,
        "progress": progress,
        "reply_to_message_id": task.message_id,
    }
    if media_type == "video":
        return await client.send_video(
            video=str(item),
            supports_streaming=True,
            no_sound=False,
            **common,
        )
    if media_type == "audio":
        return await client.send_audio(audio=str(item), **common)
    if media_type == "photo":
        return await client.send_photo(photo=str(item), **common)
    if media_type == "animation":
        return await client.send_animation(animation=str(item), **common)
    return await client.send_document(
        document=str(item),
        force_document=True,
        **common,
    )


async def split_file(
    task: Task, source: Path, parts_dir: Path, part_size: int
) -> list[Path]:
    LOGGER.info(
        "Task %s: splitting Telegram file name=%r size=%s part_size=%s",
        task.short_id(),
        source.name,
        source.stat().st_size,
        part_size,
    )
    key = sha1(str(source).encode(), usedforsecurity=False).hexdigest()[:12]
    target_dir = parts_dir / key
    target_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    index = 1
    task.phase = TaskPhase.SPLITTING
    task.current_file = source.name
    task.size = source.stat().st_size
    task.downloaded = 0
    task.progress = 0
    task.speed = 0
    task.eta = 0
    started = monotonic()
    async with aiofiles.open(source, "rb") as input_file:
        while True:
            if task.cancelled:
                raise asyncio.CancelledError()
            part = target_dir / f"{source.name}.part{index:03d}"
            written = 0
            async with aiofiles.open(part, "wb") as output_file:
                while written < part_size:
                    if task.cancelled:
                        raise asyncio.CancelledError()
                    chunk = await input_file.read(min(8 * 1024 * 1024, part_size - written))
                    if not chunk:
                        break
                    await output_file.write(chunk)
                    written += len(chunk)
                    task.downloaded += len(chunk)
                    task.progress = task.downloaded / task.size if task.size else 0
                    elapsed = monotonic() - started
                    task.speed = int(task.downloaded / elapsed) if elapsed else 0
                    task.eta = (
                        int((task.size - task.downloaded) / task.speed)
                        if task.speed
                        else 0
                    )
            if not written:
                part.unlink(missing_ok=True)
                break
            parts.append(part)
            index += 1
            if written < part_size:
                break
    LOGGER.info(
        "Task %s: split complete name=%r parts=%s",
        task.short_id(),
        source.name,
        len(parts),
    )
    return parts


async def upload_to_telegram(
    task: Task,
    path: Path,
    client: Client,
    split_size: int,
) -> int:
    files = upload_files(path)
    if not files:
        raise RuntimeError("Nothing to upload to Telegram")

    parts_dir = task.work_dir / ".telegram-parts"
    total_size = path_size(path)
    task.size = total_size
    task.downloaded = 0
    task.progress = 0
    task.speed = 0
    task.eta = 0
    started = monotonic()
    uploaded = 0
    sent = 0
    task.result_files = []
    task.result_folders = []
    task.result_links = []

    try:
        for source, relative_name in files:
            if task.cancelled:
                raise asyncio.CancelledError()
            if source.stat().st_size > split_size:
                outgoing = await split_file(task, source, parts_dir, split_size)
                task.phase = TaskPhase.UPLOADING
                task.size = total_size
                task.downloaded = uploaded
                task.progress = uploaded / total_size if total_size else 0
                task.speed = 0
                task.eta = 0
            else:
                outgoing = [source]

            for index, item in enumerate(outgoing, start=1):
                if task.cancelled:
                    raise asyncio.CancelledError()
                part_suffix = (
                    f" ({index}/{len(outgoing)})" if len(outgoing) > 1 else ""
                )
                current_file = f"{relative_name}{part_suffix}"
                task.current_file = current_file

                async def progress(current: int, _total: int):
                    if task.cancelled:
                        client.stop_transmission()
                    task.downloaded = min(total_size, uploaded + current)
                    task.progress = task.downloaded / total_size if total_size else 0
                    elapsed = monotonic() - started
                    task.speed = int(task.downloaded / elapsed) if elapsed else 0
                    task.eta = (
                        int((total_size - task.downloaded) / task.speed)
                        if task.speed
                        else 0
                    )

                caption = current_file[:1024]
                media_type = (
                    telegram_media_type(item) if len(outgoing) == 1 else "document"
                )
                LOGGER.info(
                    "Task %s: uploading Telegram file name=%r size=%s type=%s",
                    task.short_id(),
                    current_file,
                    item.stat().st_size,
                    media_type,
                )
                message = await send_telegram_file(
                    client,
                    task,
                    item,
                    caption,
                    progress,
                    media_type,
                )
                if message is None:
                    raise asyncio.CancelledError()
                uploaded += item.stat().st_size
                task.downloaded = min(total_size, uploaded)
                task.progress = task.downloaded / total_size if total_size else 1
                sent += 1
                task.result_files.append(current_file)
                task.result_links.append(
                    message.link
                    or (
                        f"tg://openmessage?user_id={task.chat_id}"
                        f"&message_id={message.id}"
                    )
                )
                LOGGER.info(
                    "Task %s: Telegram upload complete name=%r",
                    task.short_id(),
                    current_file,
                )

        task.progress = 1
        task.downloaded = total_size
        task.eta = 0
        LOGGER.info(
            "Task %s: Telegram delivery complete files=%s bytes=%s",
            task.short_id(),
            sent,
            total_size,
        )
        return sent
    finally:
        rmtree(parts_dir, ignore_errors=True)
