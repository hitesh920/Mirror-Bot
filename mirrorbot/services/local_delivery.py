import asyncio
import os
from pathlib import Path
from shutil import rmtree
from time import monotonic

import aiofiles

from ..core.models import Task
from ..downloaders.process import path_size
from .paths import ensure_inside, local_category_root
from .media_library import MediaMatch, apply_media_permissions, clean_release_title, media_target


async def deliver_to_local(
    task: Task, downloaded: Path, local_root: Path, category: str, match: MediaMatch
) -> Path:
    root = local_category_root(local_root, category)
    target = media_target(local_root, category, downloaded, match)
    if downloaded.is_dir() and category == "series" and match.season is not None:
        target = target.parent
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
                if item.is_file() and category == "series":
                    season = clean_release_title(item.name)[2]
                    if season is not None:
                        relative = Path(f"Season {season:02d}") / item.name
                destination = staging / relative
                ensure_inside(local_root, destination)
                if item.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif item.is_file():
                    if destination.exists():
                        raise FileExistsError(f"Duplicate local delivery name: {destination}")
                    await _copy_file(task, item, destination, started)
        if task.cancelled:
            raise asyncio.CancelledError()
        await asyncio.to_thread(_commit_staging, staging, target)
        if downloaded.is_dir():
            rmtree(downloaded, ignore_errors=True)
        else:
            downloaded.unlink(missing_ok=True)
        await asyncio.to_thread(apply_media_permissions, local_root, target)
        task.downloaded = task.size
        task.progress = 1
        task.eta = 0
        return target
    finally:
        rmtree(staging, ignore_errors=True)


async def _copy_file(task: Task, source: Path, destination: Path, started: float) -> None:
    task.current_file = source.name
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
    _assert_no_conflicts(staging, target)
    target.mkdir(parents=True, exist_ok=True)
    for item in staging.iterdir():
        destination = target / item.name
        if item.is_dir():
            _commit_staging(item, destination)
            item.rmdir()
        else:
            os.replace(item, destination)


def _assert_no_conflicts(staging: Path, target: Path) -> None:
    if not target.exists():
        return
    for item in staging.rglob("*"):
        destination = target / item.relative_to(staging)
        if not destination.exists():
            continue
        if item.is_file() or item.is_dir() != destination.is_dir():
            raise FileExistsError(f"Local destination already exists: {destination}")
