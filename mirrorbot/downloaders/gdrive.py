import asyncio
import json
from pathlib import Path
from time import monotonic

from ..models import Task, TaskPhase
from ..resolvers.base import safe_name
from .process import path_size, terminate_process


def is_drive_folder(url: str) -> bool:
    return "/folders/" in url


def gdown_command(url: str, work_dir: Path) -> list[str]:
    command = [
        "python",
        "-m",
        "mirrorbot.gdown_worker",
        url,
        f"{work_dir}/",
    ]
    if is_drive_folder(url):
        command.append("--folder")
    return command


def downloaded_result(
    work_dir: Path, fallback_name: str, preserve_folder: bool = False
) -> Path:
    children = list(work_dir.iterdir())
    if not children:
        raise RuntimeError("Google Drive download completed without files")
    if len(children) == 1 and (not preserve_folder or children[0].is_dir()):
        return children[0]
    root = work_dir / safe_name(fallback_name, "Google Drive")
    index = 2
    while root.exists() and not root.is_dir():
        root = work_dir / f"{safe_name(fallback_name, 'Google Drive')} ({index})"
        index += 1
    root.mkdir(exist_ok=True)
    for child in children:
        if child != root:
            child.rename(root / child.name)
    return root


async def download_gdrive(task: Task) -> Path:
    task.work_dir.mkdir(parents=True, exist_ok=True)
    task.phase = TaskPhase.METADATA
    command = gdown_command(task.source.value, task.work_dir)
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    monitor = asyncio.create_task(monitor_gdrive(task, process))
    await process.wait()
    await monitor
    if process.returncode:
        detail = (await process.stderr.read()).decode(errors="replace").strip()
        raise RuntimeError(f"Google Drive download failed: {detail[-1000:]}")
    task.downloaded = path_size(task.work_dir)
    task.size = task.downloaded
    task.progress = 1
    task.eta = 0
    result = downloaded_result(
        task.work_dir,
        task.options.name,
        preserve_folder=is_drive_folder(task.source.value),
    )
    task.name = result.name
    return result


async def monitor_gdrive(task: Task, process: asyncio.subprocess.Process) -> None:
    started = monotonic()
    while True:
        if task.cancelled:
            await terminate_process(process)
            raise asyncio.CancelledError()
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=1)
        except asyncio.TimeoutError:
            line = b""
        current = path_size(task.work_dir)
        if line:
            try:
                progress = json.loads(line)
            except json.JSONDecodeError:
                progress = {}
            if progress.get("metadata"):
                task.name = safe_name(progress.get("name", ""), "Google Drive")
                task.phase = TaskPhase.DOWNLOADING
            current = max(current, int(progress.get("current") or 0))
            total = int(progress.get("total") or 0)
            if total:
                task.size = total
        task.downloaded = current
        elapsed = monotonic() - started
        task.speed = int(current / elapsed) if elapsed else 0
        if task.size and current > task.size:
            task.size = 0
            task.progress = 0
            task.eta = 0
        if task.size:
            task.progress = min(current / task.size, 1)
            task.eta = (
                int((task.size - current) / task.speed) if task.speed else 0
            )
        if process.returncode is not None and not line:
            break
