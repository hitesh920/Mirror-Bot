import asyncio
import os
import signal
from pathlib import Path

from ..core.models import Task


def path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def monitor_directory(task: Task, process: asyncio.subprocess.Process) -> None:
    previous = 0
    while process.returncode is None:
        if task.cancelled:
            await terminate_process(process)
            raise asyncio.CancelledError()
        current = path_size(task.work_dir)
        task.downloaded = current
        if task.size:
            task.progress = min(current / task.size, 1)
        delta = max(0, current - previous)
        task.speed = delta
        if task.size and task.speed:
            task.eta = int((task.size - current) / task.speed)
        previous = current
        await asyncio.sleep(1)
