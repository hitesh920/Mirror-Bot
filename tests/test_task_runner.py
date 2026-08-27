"""Coverage for the main transfer pipeline: run, cancel, failure categories (#26)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mirrorbot.core.errors import DiskSpaceError, TaskFailure
from mirrorbot.core.models import Destination, TaskPhase
from mirrorbot.services import task_runner as tr_module
from mirrorbot.services.archive import ArchiveCorruptError
from mirrorbot.services.task_manager import TaskManager
from mirrorbot.services.task_runner import TaskRunner


class FakeGuard:
    def __init__(self, task):
        self.task = task

    async def monitor(self):
        return None


@pytest.fixture
def manager(tmp_path):
    config = SimpleNamespace(
        task_limit=4,
        qb_host="http://qb:8080",
        torrent_selection_port=8001,
        torrent_selection_timeout=300,
        public_base_url="http://test:8001",
        download_dir=tmp_path / "downloads",
        zip_compression_level=5,
        telegram_leech_split_size=2_000_000_000,
        telegram_dump_chat_id="",
    )
    mgr = TaskManager(config)
    mgr.qb = SimpleNamespace(delete=AsyncMock(), close=AsyncMock())
    return mgr


@pytest.fixture(autouse=True)
def _patch_pipeline(monkeypatch):
    async def _identity(source):
        return source

    monkeypatch.setattr(tr_module, "resolve_source", _identity)
    monkeypatch.setattr(tr_module, "ensure_disk_space", lambda *a, **k: None)
    monkeypatch.setattr(tr_module, "ensure_no_symlinks", lambda *a, **k: None)
    monkeypatch.setattr(tr_module, "TransferGuard", FakeGuard)
    monkeypatch.setattr(tr_module, "upload_to_r2", AsyncMock())
    monkeypatch.setattr(tr_module, "upload_to_telegram", AsyncMock(return_value=1))


def _stub_download(manager, tmp_path, name="file.bin"):
    async def _download(task, *a, **k):
        task.work_dir.mkdir(parents=True, exist_ok=True)
        path = task.work_dir / name
        path.write_bytes(b"payload")
        return path

    manager._download = _download


async def test_happy_path_completes_and_uploads(manager, make_task, tmp_path):
    _stub_download(manager, tmp_path)
    task = make_task(destination=Destination.CLOUDFLARE_R2)

    await TaskRunner(manager).run_task(task)

    assert task.phase == TaskPhase.COMPLETE
    tr_module.upload_to_r2.assert_awaited_once()
    assert not task.work_dir.exists()  # _finalize cleaned up


async def test_cancellation_marks_task_cancelled(manager, make_task):
    async def _download(task, *a, **k):
        task.request_cancel("user")
        raise asyncio.CancelledError()

    manager._download = _download
    task = make_task()

    await TaskRunner(manager).run_task(task)

    assert task.phase == TaskPhase.CANCELLED
    assert task.cancelled is True
    tr_module.upload_to_r2.assert_not_awaited()


async def test_task_failure_records_category(manager, make_task, monkeypatch):
    async def _boom(source):
        raise TaskFailure("engine exploded")

    monkeypatch.setattr(tr_module, "resolve_source", _boom)
    task = make_task()

    await TaskRunner(manager).run_task(task)

    assert task.phase == TaskPhase.ERROR
    assert task.failure_category == "engine"
    assert "engine exploded" in task.error


async def test_archive_error_is_processing_category(
    manager, make_task, tmp_path, monkeypatch
):
    _stub_download(manager, tmp_path, name="bundle.zip")

    async def _extract(path, task, password=""):
        raise ArchiveCorruptError("truncated archive")

    monkeypatch.setattr(tr_module, "extract_path", _extract)
    task = make_task()
    task.options.extract = True

    await TaskRunner(manager).run_task(task)

    assert task.phase == TaskPhase.ERROR
    assert task.failure_category == "processing"


async def test_unexpected_error_is_categorised_unexpected(manager, make_task):
    async def _download(task, *a, **k):
        raise ValueError("something odd")

    manager._download = _download
    task = make_task()

    await TaskRunner(manager).run_task(task)

    assert task.phase == TaskPhase.ERROR
    assert task.failure_category == "unexpected"


async def test_guard_failure_surfaces_as_categorised_failure(
    manager, make_task, tmp_path
):
    async def _download(task, *a, **k):
        task.fail_guard(DiskSpaceError("disk full"))
        task.work_dir.mkdir(parents=True, exist_ok=True)
        return task.work_dir / "x"

    manager._download = _download
    task = make_task()

    await TaskRunner(manager).run_task(task)

    assert task.phase == TaskPhase.ERROR
    assert task.failure_category == "disk"
    tr_module.upload_to_r2.assert_not_awaited()
