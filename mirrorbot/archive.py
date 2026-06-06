import logging
from asyncio import create_subprocess_exec
from asyncio.subprocess import PIPE
from pathlib import Path
from shutil import make_archive, unpack_archive

LOGGER = logging.getLogger(__name__)


async def _run(*args: str) -> None:
    proc = await create_subprocess_exec(*args, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await proc.communicate()
    code = proc.returncode
    if code != 0:
        detail = stderr.decode(errors="replace").strip() or stdout.decode(
            errors="replace"
        ).strip()
        LOGGER.error("Archive command failed command=%s detail=%s", args[0], detail)
        raise RuntimeError(f"Archive command failed: {args[0]}")


async def zip_path(path: Path, password: str = "") -> Path:
    if password:
        output = path.with_suffix(path.suffix + ".zip") if path.is_file() else path.with_suffix(".zip")
        await _run("7z", "a", f"-p{password}", "-mem=AES256", str(output), str(path))
        return output
    archive_base = str(path.with_suffix("")) if path.is_file() else str(path)
    result = make_archive(archive_base, "zip", path.parent, path.name)
    return Path(result)


async def extract_path(path: Path, password: str = "") -> Path:
    output_dir = path.parent / path.stem
    output_dir.mkdir(parents=True, exist_ok=True)
    if password:
        await _run("7z", "x", f"-p{password}", f"-o{output_dir}", str(path))
    else:
        unpack_archive(str(path), str(output_dir))
    return output_dir
