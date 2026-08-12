import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mirrorbot.core.errors import DiskSpaceError, StalledTransferError
from mirrorbot.core.models import (
    AddOptions,
    Destination,
    Source,
    SourceType,
    Task,
    TaskPhase,
)
from mirrorbot.downloaders import telegram as telegram_downloader
from mirrorbot.services import task_runner as task_runner_module
from mirrorbot.services.task_manager import TaskManager
from mirrorbot.services.task_runner import TaskRunner
from mirrorbot.services.transfer_guard import (
    STALL_TIMEOUT,
    DiskReservationPool,
    TransferGuard,
)


class RecordingEvent(asyncio.Event):
    def __init__(self):
        super().__init__()
        self.waiter = None
        self.exited = asyncio.Event()

    async def wait(self):
        self.waiter = asyncio.current_task()
        try:
            return await super().wait()
        finally:
            self.exited.set()


class RecordingSemaphore(asyncio.Semaphore):
    def __init__(self, value: int):
        super().__init__(value)
        self.acquire_started = asyncio.Event()

    async def acquire(self):
        self.acquire_started.set()
        return await super().acquire()


def make_task(tmp_path: Path) -> Task:
    return Task(
        id="00000000-0000-0000-0000-000000000001",
        user_id=1,
        chat_id=1,
        message_id=1,
        source=Source(SourceType.DIRECT_URL, "https://example.com/file.bin"),
        destination=Destination.TELEGRAM,
        options=AddOptions(),
        work_dir=tmp_path / "task",
    )


@pytest.mark.asyncio
async def test_run_or_cancel_cleans_children_when_caller_is_cancelled(tmp_path):
    manager = TaskManager.__new__(TaskManager)
    task = make_task(tmp_path)
    task.cancel_event = RecordingEvent()
    started = asyncio.Event()
    finalized = asyncio.Event()
    operation_task = None

    async def operation():
        nonlocal operation_task
        operation_task = asyncio.current_task()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    runner = asyncio.create_task(manager._run_or_cancel(task, operation()))
    await started.wait()
    runner.cancel()

    with pytest.raises(asyncio.CancelledError):
        await runner

    await asyncio.wait_for(finalized.wait(), 0.2)
    await asyncio.wait_for(task.cancel_event.exited.wait(), 0.2)
    assert operation_task.done()
    assert task.cancel_event.waiter.done()


@pytest.mark.asyncio
async def test_run_or_cancel_closes_unscheduled_coroutine(tmp_path):
    manager = TaskManager.__new__(TaskManager)
    task = make_task(tmp_path)
    task.request_cancel()

    async def operation():
        await asyncio.sleep(0)

    coroutine = operation()
    with pytest.raises(asyncio.CancelledError):
        await manager._run_or_cancel(task, coroutine)

    assert coroutine.cr_frame is None


@pytest.mark.asyncio
async def test_run_or_cancel_drains_children_after_second_cancellation(tmp_path):
    manager = TaskManager.__new__(TaskManager)
    task = make_task(tmp_path)
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    finalized = asyncio.Event()

    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await allow_cleanup.wait()
            finalized.set()

    runner = asyncio.create_task(manager._run_or_cancel(task, operation()))
    await started.wait()
    runner.cancel()
    await cleanup_started.wait()
    runner.cancel()
    allow_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await runner

    assert finalized.is_set()


@pytest.mark.asyncio
async def test_queue_slot_cancellation_does_not_consume_future_permit(tmp_path):
    manager = TaskManager.__new__(TaskManager)
    task = make_task(tmp_path)
    task.cancel_event = RecordingEvent()
    semaphore = RecordingSemaphore(0)

    async def queued():
        async with manager._queue_slot(semaphore, task):
            pytest.fail("cancelled queue entry must not run")

    waiter = asyncio.create_task(queued())
    await semaphore.acquire_started.wait()
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    await asyncio.wait_for(task.cancel_event.exited.wait(), 0.2)
    assert task.cancel_event.waiter.done()
    semaphore.release()
    await asyncio.wait_for(semaphore.acquire(), 0.2)


@pytest.mark.asyncio
async def test_queue_slot_simultaneous_cancel_and_permit_returns_permit(tmp_path):
    manager = TaskManager.__new__(TaskManager)
    task = make_task(tmp_path)
    semaphore = RecordingSemaphore(0)

    async def queued():
        async with manager._queue_slot(semaphore, task):
            pytest.fail("cancelled queue entry must not run")

    waiter = asyncio.create_task(queued())
    await semaphore.acquire_started.wait()
    task.request_cancel()
    semaphore.release()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert semaphore._value == 1
    await asyncio.wait_for(semaphore.acquire(), 0.2)


def test_phase_change_resets_stall_progress_baselines(tmp_path):
    task = make_task(tmp_path)
    task.phase = TaskPhase.DOWNLOADING
    task.downloaded = 10_000
    task.progress = 1
    guard = TransferGuard(task)
    started = guard.last_activity

    task.phase = TaskPhase.UPLOADING
    task.downloaded = 0
    task.progress = 0
    assert not guard.check_progress(started + 1)

    task.downloaded = 1
    task.progress = 0.01
    progress_at = started + STALL_TIMEOUT + 2
    assert not guard.check_progress(progress_at)

    assert task.guard_error is None
    assert guard.last_bytes == 1
    assert guard.last_progress == 0.01
    assert guard.check_progress(progress_at + STALL_TIMEOUT)
    assert isinstance(task.guard_error, StalledTransferError)


@pytest.mark.asyncio
async def test_disk_reservations_are_atomic_across_tasks(monkeypatch, tmp_path):
    usage = SimpleNamespace(total=1_000, used=750, free=250)
    monkeypatch.setattr("mirrorbot.services.transfer_guard.MIN_RESERVE", 100)
    monkeypatch.setattr("mirrorbot.services.transfer_guard.RESERVE_RATIO", 0)
    monkeypatch.setattr(
        "mirrorbot.services.transfer_guard.shutil.disk_usage",
        lambda _path: usage,
    )
    pool = DiskReservationPool()

    results = await asyncio.gather(
        pool.reserve("first", tmp_path, 100),
        pool.reserve("second", tmp_path, 100),
        return_exceptions=True,
    )
    winner = "first" if results[0] is None else "second"
    loser = "second" if winner == "first" else "first"

    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, DiskSpaceError) for result in results) == 1
    assert await pool.reserved() == 100
    await pool.release(winner)
    await pool.reserve(loser, tmp_path, 100)
    assert await pool.reserved() == 100


@pytest.mark.asyncio
async def test_disk_reservation_tracks_remaining_bytes(monkeypatch, tmp_path):
    usage = SimpleNamespace(total=1_000, used=750, free=250)
    monkeypatch.setattr("mirrorbot.services.transfer_guard.MIN_RESERVE", 100)
    monkeypatch.setattr("mirrorbot.services.transfer_guard.RESERVE_RATIO", 0)
    monkeypatch.setattr(
        "mirrorbot.services.transfer_guard.shutil.disk_usage",
        lambda _path: usage,
    )
    pool = DiskReservationPool()

    await pool.reserve("first", tmp_path, 100)
    usage.free = 190
    await pool.update_remaining("first", tmp_path, 40)
    await pool.reserve("second", tmp_path, 50)

    assert await pool.reserved() == 90


@pytest.mark.asyncio
async def test_telegram_known_size_is_reserved_before_download_starts(
    tmp_path,
    monkeypatch,
):
    task = make_task(tmp_path)
    reserved = asyncio.Event()

    async def reserve(_task, _path, required):
        assert required == 123
        reserved.set()

    async def download(**kwargs):
        assert reserved.is_set()
        assert kwargs["file_name"].endswith("document.bin")
        return kwargs["file_name"]

    message = SimpleNamespace(
        id=7,
        document=SimpleNamespace(file_name="document.bin", file_size=123),
        download=download,
    )
    monkeypatch.setattr(telegram_downloader, "reserve_disk_space", reserve)

    result = await telegram_downloader.download_telegram_file(task, message)

    assert result.name == "document.bin"
    assert task.size == 123


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "cleanup_fails"),
    [
        (TaskPhase.COMPLETE, False),
        (TaskPhase.CANCELLED, True),
        (TaskPhase.ERROR, True),
    ],
)
async def test_finalization_releases_disk_reservation(
    tmp_path,
    phase,
    cleanup_fails,
):
    task = make_task(tmp_path)
    task.phase = phase
    pool = DiskReservationPool()
    pool._remaining[task.id] = 100
    task.disk_reservation_pool = pool
    cleanup = AsyncMock(
        side_effect=OSError("workspace is locked") if cleanup_fails else None
    )
    manager = SimpleNamespace(
        _cleanup=cleanup,
        _prune_terminal_tasks=lambda: None,
        qb=SimpleNamespace(delete=AsyncMock()),
    )

    await TaskRunner(manager)._finalize(task, None)

    assert await pool.reserved(task.id) == 0


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_finalization(tmp_path):
    task = make_task(tmp_path)
    task.phase = TaskPhase.CANCELLED
    pool = DiskReservationPool()
    pool._remaining[task.id] = 100
    task.disk_reservation_pool = pool
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()

    async def cleanup(_path):
        cleanup_started.set()
        await allow_cleanup.wait()

    manager = SimpleNamespace(
        _cleanup=cleanup,
        _prune_terminal_tasks=lambda: None,
        qb=SimpleNamespace(delete=AsyncMock()),
    )
    finalization = asyncio.create_task(
        TaskRunner(manager)._complete_finalization(task, None)
    )
    await cleanup_started.wait()
    finalization.cancel()
    finalization.cancel()
    await asyncio.sleep(0)
    assert not finalization.done()
    allow_cleanup.set()

    await finalization

    assert await pool.reserved(task.id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secondary_error",
    [asyncio.CancelledError(), RuntimeError("secondary engine error")],
)
async def test_guard_failure_is_not_reported_as_user_cancellation(
    tmp_path,
    secondary_error,
):
    task = make_task(tmp_path)
    task.work_dir.mkdir()

    @asynccontextmanager
    async def queue_slot(_semaphore, _task):
        yield

    async def run_or_cancel(_task, awaitable):
        awaitable.close()
        task.fail_guard(DiskSpaceError("disk reserve reached"))
        raise secondary_error

    manager = SimpleNamespace(
        task_sem=asyncio.Semaphore(1),
        _queue_slot=queue_slot,
        _raise_if_cancelled=lambda _task: None,
        _run_or_cancel=run_or_cancel,
        _cleanup=AsyncMock(),
        _prune_terminal_tasks=lambda: None,
        qb=SimpleNamespace(delete=AsyncMock()),
    )

    result = await TaskRunner(manager).run_task(task)

    assert result.phase == TaskPhase.ERROR
    assert result.failure_category == "disk"
    assert result.error == "disk reserve reached"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [DiskSpaceError("disk reserve reached"), StalledTransferError("stalled")],
)
async def test_processing_guard_failure_does_not_fall_back_to_archive(
    tmp_path,
    monkeypatch,
    error,
):
    task = make_task(tmp_path)
    task.options.extract = True
    archive_path = tmp_path / "archive.zip"
    archive_path.write_bytes(b"archive")
    manager = SimpleNamespace(
        _start_processing_phase=AsyncMock(),
        _raise_if_cancelled=lambda _task: None,
        config=SimpleNamespace(zip_compression_level=5),
    )
    monkeypatch.setattr(
        task_runner_module,
        "extract_path",
        AsyncMock(side_effect=error),
    )

    with pytest.raises(type(error), match=str(error)):
        await TaskRunner(manager)._process_download(task, archive_path)

    assert not task.processing_warnings
