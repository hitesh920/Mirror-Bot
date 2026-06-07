import asyncio
from hashlib import sha1
import logging
from pathlib import Path
from shutil import rmtree
from time import monotonic

import aiofiles
from pyrogram import Client
from pyrogram.enums import ParseMode

from ..core.models import Task
from ..downloaders.process import path_size

LOGGER = logging.getLogger(__name__)


def upload_files(path: Path) -> list[tuple[Path, str]]:
    if path.is_file():
        return [(path, path.name)]
    return [
        (item, item.relative_to(path).as_posix())
        for item in sorted(path.rglob("*"))
        if item.is_file()
    ]


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

    try:
        for source, relative_name in files:
            if task.cancelled:
                raise asyncio.CancelledError()
            if source.stat().st_size > split_size:
                outgoing = await split_file(task, source, parts_dir, split_size)
            else:
                outgoing = [source]

            for index, item in enumerate(outgoing, start=1):
                if task.cancelled:
                    raise asyncio.CancelledError()
                part_suffix = (
                    f" ({index}/{len(outgoing)})" if len(outgoing) > 1 else ""
                )
                task.name = f"{relative_name}{part_suffix}"

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

                caption = task.name[:1024]
                LOGGER.info(
                    "Task %s: uploading Telegram file name=%r size=%s",
                    task.short_id(),
                    task.name,
                    item.stat().st_size,
                )
                message = await client.send_document(
                    task.chat_id,
                    str(item),
                    caption=caption,
                    parse_mode=ParseMode.DISABLED,
                    force_document=True,
                    disable_notification=True,
                    progress=progress,
                )
                if message is None:
                    raise asyncio.CancelledError()
                uploaded += item.stat().st_size
                task.downloaded = min(total_size, uploaded)
                task.progress = task.downloaded / total_size if total_size else 1
                sent += 1
                LOGGER.info(
                    "Task %s: Telegram upload complete name=%r",
                    task.short_id(),
                    task.name,
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
