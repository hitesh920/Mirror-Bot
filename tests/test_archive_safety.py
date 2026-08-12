import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mirrorbot.services import archive


class FakeStream:
    def __init__(self, payload: bytes = b""):
        self.chunks = [payload, b""]

    async def read(self, _size: int) -> bytes:
        return self.chunks.pop(0)


def make_task(*, cancelled: bool = False):
    return SimpleNamespace(
        cancelled=cancelled,
        progress=0,
        downloaded=0,
        size=0,
    )


def install_process(
    monkeypatch,
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int | None = 0,
):
    process = SimpleNamespace(
        returncode=returncode,
        stdout=FakeStream(stdout),
        stderr=FakeStream(stderr),
    )
    create_process = AsyncMock(return_value=process)
    monkeypatch.setattr(archive.asyncio, "create_subprocess_exec", create_process)
    return process, create_process


def technical_entry(path: str, size: int = 1, *extra: str) -> bytes:
    fields = [f"Path = {path}", "Folder = -", f"Size = {size}", *extra]
    return ("\n".join(fields) + "\n\n").encode()


@pytest.mark.asyncio
async def test_preflight_accepts_safe_entries_and_totals_declared_file_bytes(
    monkeypatch,
):
    listing = (
        b"Path = nested\nFolder = +\nSize = 999\n\n"
        + technical_entry("nested/first.bin", 12)
        + technical_entry(r"nested\second.bin", 30)
    )
    _, create_process = install_process(monkeypatch, stdout=listing)

    total = await archive._preflight_archive(
        Path("download/archive.zip"),
        make_task(),
    )

    assert total == 42
    create_process.assert_awaited_once_with(
        "7z",
        "l",
        "-slt",
        "-ba",
        "-sccUTF-8",
        "-p-",
        str(Path("download/archive.zip")),
        cwd=None,
        stdout=archive.PIPE,
        stderr=archive.PIPE,
        start_new_session=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "member_path",
    [
        "../escape.bin",
        "safe/../../escape.bin",
        r"safe\..\escape.bin",
    ],
)
async def test_preflight_rejects_parent_traversal(monkeypatch, member_path):
    install_process(monkeypatch, stdout=technical_entry(member_path))

    with pytest.raises(archive.ArchiveCorruptError, match="unsafe path"):
        await archive._preflight_archive(Path("archive.zip"), make_task())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "member_path",
    [
        "/etc/passwd",
        r"\Windows\System32\payload.dll",
        r"C:\Windows\payload.dll",
        r"D:relative-but-drive-qualified.bin",
    ],
)
async def test_preflight_rejects_absolute_and_drive_paths(monkeypatch, member_path):
    install_process(monkeypatch, stdout=technical_entry(member_path))

    with pytest.raises(archive.ArchiveCorruptError, match="unsafe path"):
        await archive._preflight_archive(Path("archive.zip"), make_task())


@pytest.mark.asyncio
@pytest.mark.parametrize("indicator", ["Symbolic Link", "Hard Link"])
async def test_preflight_rejects_link_indicators(monkeypatch, indicator):
    listing = technical_entry("safe/link", 4, f"{indicator} = ../target")
    install_process(monkeypatch, stdout=listing)

    with pytest.raises(archive.ArchiveCorruptError, match="symbolic or hard link"):
        await archive._preflight_archive(Path("archive.tar"), make_task())


@pytest.mark.asyncio
async def test_preflight_rejects_unix_symlink_mode(monkeypatch):
    install_process(
        monkeypatch,
        stdout=technical_entry("safe/link", 4, "Mode = lrwxrwxrwx"),
    )

    with pytest.raises(archive.ArchiveCorruptError, match="symbolic link"):
        await archive._preflight_archive(Path("archive.tar"), make_task())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("detail", "error_type"),
    [
        (b"Wrong password", archive.ArchivePasswordError),
        (b"Unsupported Method", archive.ArchiveUnsupportedError),
        (b"Unexpected end of archive", archive.ArchiveCorruptError),
        (b"7-Zip could not open this input", archive.ArchiveCommandError),
    ],
)
async def test_preflight_classifies_command_failures(
    monkeypatch,
    detail,
    error_type,
):
    install_process(monkeypatch, stderr=detail, returncode=2)

    with pytest.raises(error_type):
        await archive._preflight_archive(Path("archive.zip"), make_task())


@pytest.mark.asyncio
async def test_preflight_terminates_7z_when_cancelled(monkeypatch):
    process, _ = install_process(monkeypatch, returncode=None)

    async def terminate(candidate):
        assert candidate is process
        process.returncode = -15

    terminate_mock = AsyncMock(side_effect=terminate)
    monkeypatch.setattr(archive, "terminate_process", terminate_mock)

    with pytest.raises(asyncio.CancelledError):
        await archive._preflight_archive(
            Path("archive.zip"),
            make_task(cancelled=True),
        )

    terminate_mock.assert_awaited_once_with(process)


@pytest.mark.asyncio
async def test_extract_reserves_declared_unpacked_size_before_extraction(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "archive.zip"
    source.write_bytes(b"compressed")
    output_dir = tmp_path / "archive"
    events = []

    async def preflight(path, task, password):
        assert path == source
        assert password == "secret"
        assert task is not None
        events.append("preflight")
        return 12_345

    async def reserve(task, path, required):
        assert path == output_dir
        assert required == 12_345
        assert not output_dir.exists()
        assert task is not None
        events.append("reserve")

    async def update(task, remaining):
        assert task is not None
        assert remaining == 0
        events.append("clear")

    async def run(_task, *_args, **_kwargs):
        assert output_dir.is_dir()
        events.append("extract")

    monkeypatch.setattr(archive, "_preflight_archive", preflight)
    monkeypatch.setattr(archive, "reserve_disk_space", reserve)
    monkeypatch.setattr(archive, "update_disk_reservation", update)
    monkeypatch.setattr(archive, "_run", run)

    result = await archive.extract_path(source, make_task(), "secret")

    assert result == output_dir
    assert events == ["preflight", "reserve", "extract", "clear"]


@pytest.mark.asyncio
async def test_zip_reserves_source_size_before_replacing_output(tmp_path, monkeypatch):
    source = tmp_path / "movie.bin"
    source.write_bytes(b"source-bytes")
    output = tmp_path / "movie.zip"
    output.write_bytes(b"old-output")
    task = make_task()
    events = []

    def size(path):
        assert path == source
        events.append("size")
        return 77

    async def reserve(candidate_task, path, required):
        assert candidate_task is task
        assert path == output
        assert required == 77
        assert output.exists()
        events.append("reserve")

    async def run(candidate_task, *_args, **_kwargs):
        assert candidate_task is task
        assert not output.exists()
        events.append("archive")

    async def update(candidate_task, remaining):
        assert candidate_task is task
        assert remaining == 0
        events.append("clear")

    monkeypatch.setattr(archive, "path_size", Mock(side_effect=size))
    monkeypatch.setattr(archive, "reserve_disk_space", reserve)
    monkeypatch.setattr(archive, "_run", run)
    monkeypatch.setattr(archive, "update_disk_reservation", update)

    result = await archive.zip_path(source, task)

    assert result == output
    assert events == ["size", "reserve", "archive", "clear"]


@pytest.mark.asyncio
async def test_failed_archive_keeps_reservation_for_runner_cleanup(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "movie.bin"
    source.write_bytes(b"source")
    update = AsyncMock()

    monkeypatch.setattr(archive, "path_size", Mock(return_value=10))
    monkeypatch.setattr(archive, "reserve_disk_space", AsyncMock())
    monkeypatch.setattr(
        archive,
        "_run",
        AsyncMock(side_effect=archive.ArchiveCommandError("failed")),
    )
    monkeypatch.setattr(archive, "update_disk_reservation", update)

    with pytest.raises(archive.ArchiveCommandError, match="failed"):
        await archive.zip_path(source, make_task())

    update.assert_not_awaited()
