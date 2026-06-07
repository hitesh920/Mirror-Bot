import asyncio
import os
from pathlib import Path
from shutil import rmtree
from time import monotonic

import aiofiles

from ..core.models import Task
from ..downloaders.process import path_size
from .paths import ensure_inside, local_category_root, stem_for_file


async def deliver_to_local(
    task: Task, downloaded: Path, local_root: Path, category: str
) -> Path:
    root = local_category_root(local_root, category)
    target = root / (stem_for_file(downloaded) if downloaded.is_file() else downloaded.name)
    ensure_inside(local_root, target)
    staging = root / f".mirrorbot-{task.id}"
    ensure_inside(local_root, staging)
    rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    task.size = path_size(downloaded)
    task.downloaded = 0
    task.progress = 0
    task.speed = 0
    task.eta = 0
    started = monotonic()

    try:
        if downloaded.is_file():
            await _copy_file(task, downloaded, staging / downloaded.name, started)
        else:
            for item in sorted(downloaded.rglob("*")):
                if item.is_symlink():
                    raise RuntimeError("Local delivery refuses symbolic links")
                relative = item.relative_to(downloaded)
                destination = staging / relative
                ensure_inside(local_root, destination)
                if item.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif item.is_file():
                    await _copy_file(task, item, destination, started)
        if task.cancelled:
            raise asyncio.CancelledError()
        await asyncio.to_thread(_commit_staging, staging, target)
        if downloaded.is_dir():
            rmtree(downloaded, ignore_errors=True)
        else:
            downloaded.unlink(missing_ok=True)
        task.downloaded = task.size
        task.progress = 1
        task.eta = 0
        return target
    finally:
        rmtree(staging, ignore_errors=True)


async def _copy_file(task: Task, source: Path, destination: Path, started: float) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(source, "rb") as input_file:
        async with aiofiles.open(destination, "wb") as output_file:
            while chunk := await input_file.read(1024 * 1024):
                if task.cancelled:
                    raise asyncio.CancelledError()
                await output_file.write(chunk)
                task.downloaded += len(chunk)
                elapsed = monotonic() - started
                task.speed = int(task.downloaded / elapsed) if elapsed else 0
                if task.size:
                    task.progress = min(task.downloaded / task.size, 1)
                    task.eta = (
                        int((task.size - task.downloaded) / task.speed)
                        if task.speed
                        else 0
                    )


def _commit_staging(staging: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in staging.iterdir():
        destination = target / item.name
        if item.is_dir():
            _commit_staging(item, destination)
            item.rmdir()
        else:
            os.replace(item, destination)
