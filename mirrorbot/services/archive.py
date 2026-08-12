import asyncio
import logging
import re
from asyncio.subprocess import PIPE
from pathlib import Path

from ..core.models import Task
from ..downloaders.process import path_size, terminate_process
from .paths import ensure_no_symlinks
from .transfer_guard import reserve_disk_space, update_disk_reservation

LOGGER = logging.getLogger(__name__)
PROGRESS_PATTERN = re.compile(rb"(?<!\d)(\d{1,3})%")
MAX_DIAGNOSTIC_OUTPUT = 128 * 1024
DRIVE_PATH_PATTERN = re.compile(r"^[A-Za-z]:")


class ArchivePasswordError(RuntimeError):
    pass


class ArchiveUnsupportedError(RuntimeError):
    pass


class ArchiveCorruptError(RuntimeError):
    pass


class ArchiveCommandError(RuntimeError):
    pass


def _raise_command_error(command: str, detail: str) -> None:
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
    LOGGER.error("Archive command failed command=%s detail=%s", command, detail)
    raise ArchiveCommandError(f"Archive command failed: {detail[-500:]}")


async def _run_process(
    task: Task,
    *args: str,
    cwd: Path | None = None,
    stdout_limit: int | None = MAX_DIAGNOSTIC_OUTPUT,
    report_progress: bool = False,
) -> tuple[int, bytes, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=PIPE,
            stderr=PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise ArchiveCommandError(
            f"Could not start archive command {args[0]}: {exc}"
        ) from exc

    stdout = bytearray()
    stderr = bytearray()

    async def read_stream(stream, output: bytearray, limit: int | None) -> None:
        while chunk := await stream.read(4096):
            output.extend(chunk)
            if limit is not None and len(output) > limit:
                del output[:-limit]
            if report_progress:
                matches = PROGRESS_PATTERN.findall(chunk)
                if matches:
                    percent = min(100, int(matches[-1]))
                    task.progress = percent / 100
                    task.downloaded = int(task.size * task.progress)

    readers = [
        asyncio.create_task(read_stream(process.stdout, stdout, stdout_limit)),
        asyncio.create_task(read_stream(process.stderr, stderr, MAX_DIAGNOSTIC_OUTPUT)),
    ]
    try:
        while process.returncode is None:
            if task.cancelled:
                raise asyncio.CancelledError()
            await asyncio.sleep(0.25)
        await asyncio.gather(*readers)
    except asyncio.CancelledError:
        if process.returncode is None:
            await terminate_process(process)
        await asyncio.gather(*readers, return_exceptions=True)
        raise
    return process.returncode, bytes(stdout), bytes(stderr)


async def _run(task: Task, *args: str, cwd: Path | None = None) -> None:
    returncode, stdout, stderr = await _run_process(
        task,
        *args,
        cwd=cwd,
        report_progress=True,
    )
    detail = (stdout + stderr).decode(errors="replace").strip()
    if returncode:
        _raise_command_error(args[0], detail)
    task.progress = 1
    task.downloaded = task.size


def _technical_records(output: str) -> list[dict[str, str]]:
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith("----------"):
            lines = lines[index + 1 :]
            break

    records: list[dict[str, str]] = []
    record: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            if record:
                records.append(record)
                record = {}
            continue
        key, separator, value = line.partition(" = ")
        if separator:
            if key == "Path" and "Path" in record:
                records.append(record)
                record = {}
            record[key.strip()] = value
    if record:
        records.append(record)
    return records


def _validate_archive_path(path: str) -> None:
    if (
        not path
        or "\x00" in path
        or path.startswith(("/", "\\"))
        or DRIVE_PATH_PATTERN.match(path)
        or ".." in re.split(r"[\\/]+", path)
    ):
        raise ArchiveCorruptError(
            f"Archive contains an unsafe path and was not extracted: {path!r}"
        )


def _declared_unpacked_size(output: str) -> int:
    total = 0
    for record in _technical_records(output):
        fields = {key.casefold(): value for key, value in record.items()}
        member_path = fields.get("path")
        if member_path is None:
            continue
        _validate_archive_path(member_path)
        if "symbolic link" in fields or "hard link" in fields:
            raise ArchiveCorruptError(
                "Archive contains a symbolic or hard link and was not extracted: "
                f"{member_path!r}"
            )
        if fields.get("mode", "").lstrip().startswith("l"):
            raise ArchiveCorruptError(
                "Archive contains a symbolic link and was not extracted: "
                f"{member_path!r}"
            )
        if fields.get("folder", "").strip() == "+":
            continue
        declared_size = fields.get("size")
        if declared_size is None:
            continue
        try:
            size = int(declared_size.strip())
        except ValueError as exc:
            raise ArchiveCorruptError(
                f"Archive reports an invalid size for {member_path!r}"
            ) from exc
        if size < 0:
            raise ArchiveCorruptError(
                f"Archive reports an invalid size for {member_path!r}"
            )
        total += size
    return total


async def _preflight_archive(path: Path, task: Task, password: str = "") -> int:
    command = ["7z", "l", "-slt", "-ba", "-sccUTF-8"]
    command.append(f"-p{password}" if password else "-p-")
    command.append(str(path))
    returncode, stdout, stderr = await _run_process(
        task,
        *command,
        stdout_limit=None,
    )
    detail = (stdout + stderr).decode(errors="replace").strip()
    if returncode:
        _raise_command_error(command[0], detail)
    return _declared_unpacked_size(stdout.decode(errors="replace"))


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
    await asyncio.to_thread(ensure_no_symlinks, path)
    output = path.with_suffix(".zip")
    if output == path:
        output = path.with_name(f"{path.name}.zip")
    source_size = await asyncio.to_thread(path_size, path)
    await reserve_disk_space(task, output, source_size)
    await asyncio.to_thread(output.unlink, missing_ok=True)
    command = ["7z", "a", "-tzip", f"-mx={level}", "-y", "-bsp1"]
    if password:
        command.extend([f"-p{password}", "-mem=AES256"])
    command.extend([str(output), "." if contents_only else path.name])
    await _run(task, *command, cwd=path if contents_only else path.parent)
    await update_disk_reservation(task, 0)
    return output


async def extract_path(path: Path, task: Task, password: str = "") -> Path:
    if task.cancelled:
        raise asyncio.CancelledError()
    await asyncio.to_thread(ensure_no_symlinks, path)
    output_dir = path.parent / path.stem
    unpacked_size = await _preflight_archive(path, task, password)
    await reserve_disk_space(task, output_dir, unpacked_size)
    await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
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
    await update_disk_reservation(task, 0)
    return output_dir
