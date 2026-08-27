"""Follow-up #32: archive passwords go via stdin, never argv."""

import pytest

from mirrorbot.services import archive


@pytest.fixture
def captured(monkeypatch):
    seen = {}

    async def fake_run(_task, *command, cwd=None, stdin_data=None):
        seen["command"] = list(command)
        seen["stdin_data"] = stdin_data

    monkeypatch.setattr(archive, "_run", fake_run)
    return seen


def test_password_stdin_helper():
    assert archive._password_stdin("hunter2") == b"hunter2\n"
    assert archive._password_stdin("") is None


async def test_zip_with_password_feeds_stdin_not_argv(captured, make_task, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_bytes(b"x")
    task = make_task(size=1)

    await archive.zip_path(src, task, password="s3cret", level=1)

    assert captured["stdin_data"] == b"s3cret\n"
    assert "-p" in captured["command"]
    assert not any("s3cret" in part for part in captured["command"])


async def test_extract_7z_with_password_feeds_stdin_not_argv(
    captured, make_task, tmp_path
):
    archive_file = tmp_path / "bundle.7z"
    archive_file.write_bytes(b"7z\x00")
    task = make_task(size=1)

    await archive.extract_path(archive_file, task, password="s3cret")

    assert captured["stdin_data"] == b"s3cret\n"
    assert not any("s3cret" in part for part in captured["command"])
    assert "-p-" not in captured["command"]


async def test_extract_rar_with_password_feeds_stdin_not_argv(
    captured, make_task, tmp_path
):
    archive_file = tmp_path / "bundle.rar"
    archive_file.write_bytes(b"Rar!\x00")
    task = make_task(size=1)

    await archive.extract_path(archive_file, task, password="s3cret")

    assert captured["command"][0] == "unrar"
    assert captured["stdin_data"] == b"s3cret\n"
    assert not any("s3cret" in part for part in captured["command"])


async def test_extract_without_password_still_uses_dash_p(
    captured, make_task, tmp_path
):
    archive_file = tmp_path / "plain.7z"
    archive_file.write_bytes(b"7z\x00")
    task = make_task(size=1)

    await archive.extract_path(archive_file, task)

    assert captured["stdin_data"] is None
    assert "-p-" in captured["command"]
