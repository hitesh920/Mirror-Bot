import asyncio
import json
from pathlib import Path

from ..core.models import Task
from ..resolvers.base import safe_name
from .process import terminate_process


async def run_json(*command: str) -> dict:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        raise RuntimeError(stderr.decode(errors="replace").strip()[-1000:])
    return json.loads(stdout)


async def read_rclone_progress(task: Task, process: asyncio.subprocess.Process) -> str:
    errors = []
    while True:
        if task.cancelled:
            await terminate_process(process)
            raise asyncio.CancelledError()
        try:
            line = await asyncio.wait_for(process.stderr.readline(), timeout=1)
        except asyncio.TimeoutError:
            if process.returncode is not None:
                break
            continue
        if not line:
            break
        text = line.decode(errors="replace").strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            errors.append(text)
            continue
        status = payload.get("stats")
        if not status:
            if payload.get("level") in {"error", "critical"}:
                errors.append(payload.get("msg", text))
            continue
        task.downloaded = int(status.get("bytes") or 0)
        task.speed = int(status.get("speed") or 0)
        task.eta = int(status.get("eta") or 0)
        if task.size:
            task.progress = min(task.downloaded / task.size, 1)
    return "\n".join(errors)


async def download_rclone(task: Task, config_file: Path) -> Path:
    if not config_file.is_file():
        raise RuntimeError(f"rclone config not found at {config_file}")
    source = task.source.value
    config_args = ("--config", str(config_file))
    stat, size = await asyncio.gather(
        run_json("rclone", "lsjson", "--stat", "--no-mimetype", "--no-modtime", *config_args, source),
        run_json("rclone", "size", "--json", *config_args, source),
    )
    task.size = int(size.get("bytes") or 0)
    name = safe_name(task.options.name or stat.get("Name", ""), "rclone-download")
    task.name = name
    task.work_dir.mkdir(parents=True, exist_ok=True)
    target = task.work_dir / name
    operation = "copy" if stat.get("IsDir") else "copyto"
    command = [
        "rclone",
        operation,
        source,
        str(target),
        *config_args,
        "--stats=1s",
        "--stats-one-line",
        "--stats-log-level",
        "NOTICE",
        "--use-json-log",
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    errors = await read_rclone_progress(task, process)
    await process.wait()
    if process.returncode:
        raise RuntimeError(f"rclone download failed: {errors[-1000:]}")
    task.downloaded = task.size or task.downloaded
    task.progress = 1
    task.eta = 0
    return target
