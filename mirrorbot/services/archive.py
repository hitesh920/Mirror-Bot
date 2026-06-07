import asyncio
import logging
import re
from asyncio.subprocess import PIPE
from pathlib import Path

from ..core.models import Task
from ..downloaders.process import terminate_process

LOGGER = logging.getLogger(__name__)
PROGRESS_PATTERN = re.compile(rb"(?<!\d)(\d{1,3})%")


class ArchivePasswordError(RuntimeError):
    pass


class ArchiveUnsupportedError(RuntimeError):
    pass


class ArchiveCorruptError(RuntimeError):
    pass


async def _run(task: Task, *args: str, cwd: Path | None = None) -> None:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=PIPE,
        stderr=PIPE,
    )
    output: list[bytes] = []

    async def read_stream(stream) -> None:
        while chunk := await stream.read(4096):
            output.append(chunk)
            matches = PROGRESS_PATTERN.findall(chunk)
            if matches:
                percent = min(100, int(matches[-1]))
                task.progress = percent / 100
                task.downloaded = int(task.size * task.progress)

    readers = [
        asyncio.create_task(read_stream(process.stdout)),
        asyncio.create_task(read_stream(process.stderr)),
    ]
    while process.returncode is None:
        if task.cancelled:
            await terminate_process(process)
            await asyncio.gather(*readers, return_exceptions=True)
            raise asyncio.CancelledError()
        await asyncio.sleep(0.25)
    await asyncio.gather(*readers)
    detail = b"".join(output).decode(errors="replace").strip()
    if process.returncode:
        if (
            "Break signaled" in detail
            or "Wrong password" in detail
            or "Incorrect password" in detail
        ):
            raise ArchivePasswordError(
                "Archive is password-protected or the password is incorrect. "
                "Use -ep <password>."
            )
        if "Unsupported Method" in detail:
            raise ArchiveUnsupportedError(
                "This archive uses a compression method that the installed extractor "
                "does not support."
            )
        if (
            "Attempted to read more data than was available" in detail
            or "Unexpected end of archive" in detail
            or "Unexpected end of file" in detail
            or "CRC failed" in detail
            or "checksum error" in detail.lower()
        ):
            raise ArchiveCorruptError(
                "The archive is incomplete or corrupted and could not be extracted."
            )
        LOGGER.error("Archive command failed command=%s detail=%s", args[0], detail)
        raise RuntimeError(f"Archive command failed: {detail[-500:]}")
    task.progress = 1
    task.downloaded = task.size


async def zip_path(path: Path, task: Task, password: str = "", level: int = 5) -> Path:
    if task.cancelled:
        raise asyncio.CancelledError()
    output = path.with_suffix(".zip")
    if output == path:
        output = path.with_name(f"{path.name}.zip")
    output.unlink(missing_ok=True)
    command = ["7z", "a", "-tzip", f"-mx={level}", "-y", "-bsp1"]
    if password:
        command.extend([f"-p{password}", "-mem=AES256"])
    command.extend([str(output), path.name])
    await _run(task, *command, cwd=path.parent)
    return output


async def extract_path(path: Path, task: Task, password: str = "") -> Path:
    if task.cancelled:
        raise asyncio.CancelledError()
    output_dir = path.parent / path.stem
    output_dir.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".rar":
        command = ["unrar", "x", "-o+"]
        if password:
            command.append(f"-p{password}")
        else:
            command.append("-p-")
        command.append(str(path))
        command.append(f"{output_dir}/")
    else:
        command = ["7z", "x", "-y", "-bsp1", f"-o{output_dir}"]
        if password:
            command.append(f"-p{password}")
        else:
            command.append("-p-")
        command.append(str(path))
    await _run(task, *command)
    return output_dir
