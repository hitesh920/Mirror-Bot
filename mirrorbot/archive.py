from asyncio import create_subprocess_exec
from pathlib import Path
from shutil import make_archive, unpack_archive


async def _run(*args: str) -> None:
    proc = await create_subprocess_exec(*args)
    code = await proc.wait()
    if code != 0:
        raise RuntimeError(f"Command failed: {' '.join(args)}")


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

