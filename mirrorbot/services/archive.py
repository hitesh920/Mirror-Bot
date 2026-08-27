import asyncio
import logging
import re
from asyncio.subprocess import PIPE
from contextlib import suppress
from pathlib import Path

from ..core.errors import TaskFailure
from ..core.models import Task
from ..downloaders.process import path_size, terminate_process
from .transfer_guard import ensure_disk_space

LOGGER = logging.getLogger(__name__)
PROGRESS_PATTERN = re.compile(rb"(?<!\d)(\d{1,3})%")
MAX_DIAGNOSTIC_OUTPUT = 128 * 1024


class ArchiveError(TaskFailure):
    category = "processing"


class ArchivePasswordError(ArchiveError):
    pass


class ArchiveUnsupportedError(ArchiveError):
    pass


class ArchiveCorruptError(ArchiveError):
    pass


def _password_stdin(password: str) -> bytes | None:
    """7-Zip and unrar read the password from stdin when no -p<value> is given,
    which keeps it out of argv / /proc/<pid>/cmdline."""
    return f"{password}\n".encode() if password else None


async def _run(
    task: Task, *args: str, cwd: Path | None = None, stdin_data: bytes | None = None
) -> None:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdin=PIPE if stdin_data is not None else None,
        stdout=PIPE,
        stderr=PIPE,
        start_new_session=True,
    )
    if stdin_data is not None and process.stdin is not None:
        process.stdin.write(stdin_data)
        with suppress(OSError):
            await process.stdin.drain()
        process.stdin.close()
    output = bytearray()

    async def read_stream(stream) -> None:
        while chunk := await stream.read(4096):
            output.extend(chunk)
            if len(output) > MAX_DIAGNOSTIC_OUTPUT:
                del output[:-MAX_DIAGNOSTIC_OUTPUT]
            matches = PROGRESS_PATTERN.findall(chunk)
            if matches:
                percent = min(100, int(matches[-1]))
                task.report_progress(int(task.size * percent / 100), size=task.size)

    readers = [
        asyncio.create_task(read_stream(process.stdout)),
        asyncio.create_task(read_stream(process.stderr)),
    ]
    wait_job = asyncio.create_task(process.wait())
    cancel_job = asyncio.create_task(task.cancel_event.wait())
    done, _ = await asyncio.wait(
        {wait_job, cancel_job}, return_when=asyncio.FIRST_COMPLETED
    )
    cancel_job.cancel()
    if wait_job not in done:
        await terminate_process(process)
        await asyncio.gather(
            wait_job, cancel_job, *readers, return_exceptions=True
        )
        raise asyncio.CancelledError()
    await asyncio.gather(cancel_job, *readers, return_exceptions=True)
    detail = output.decode(errors="replace").strip()
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
    task.report_progress(task.size, complete=True)


async def zip_path(
    path: Path,
    task: Task,
    password: str = "",
    level: int = 5,
    *,
    contents_only: bool = False,
) -> Path:
    if task.cancelled:
        raise asyncio.CancelledError()
    output = path.with_suffix(".zip")
    if output == path:
        output = path.with_name(f"{path.name}.zip")
    output.unlink(missing_ok=True)
    ensure_disk_space(output, path_size(path))
    command = ["7z", "a", "-tzip", f"-mx={level}", "-y", "-bsp1"]
    if password:
        command.extend(["-p", "-mem=AES256"])
    command.extend([str(output), "." if contents_only else path.name])
    await _run(
        task,
        *command,
        cwd=path if contents_only else path.parent,
        stdin_data=_password_stdin(password),
    )
    return output


async def extract_path(path: Path, task: Task, password: str = "") -> Path:
    if task.cancelled:
        raise asyncio.CancelledError()
    output_dir = path.parent / path.stem
    ensure_disk_space(output_dir, path_size(path))
    output_dir.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".rar":
        command = ["unrar", "x", "-o+"]
        if not password:
            command.append("-p-")
        command.append(str(path))
        command.append(f"{output_dir}/")
    else:
        command = ["7z", "x", "-y", "-bsp1", f"-o{output_dir}"]
        if not password:
            command.append("-p-")
        command.append(str(path))
    await _run(task, *command, stdin_data=_password_stdin(password))
    return output_dir
