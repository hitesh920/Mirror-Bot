import asyncio
from pathlib import Path

import pytest

from mirrorbot.core.models import AddOptions, Destination, Source, SourceType, Task, TaskPhase
from mirrorbot.services.task_manager import TaskManager


def make_task(tmp_path: Path, phase=TaskPhase.QUEUED):
    return Task("abc-def", 1, 1, 1, Source(SourceType.DIRECT_URL, "https://example.com/a"), Destination.TELEGRAM, AddOptions(), tmp_path / "work", phase=phase)


def test_transition_and_cancel_are_idempotent(tmp_path):
    task = make_task(tmp_path)
    task.transition(TaskPhase.DOWNLOADING, "a.bin")
    assert task.phase == TaskPhase.DOWNLOADING
    assert task.request_cancel("test") is True
    assert task.request_cancel("again") is False
    assert task.cancel_reason == "test"


@pytest.mark.asyncio
async def test_queued_cancellation_releases_semaphore(config):
    manager = TaskManager(config)
    await manager.task_sem.acquire()
    task = make_task(config.download_dir)
    waiter = asyncio.create_task(manager._queue_slot(manager.task_sem, task).__aenter__())
    await asyncio.sleep(0)
    task.request_cancel("test")
    with pytest.raises(asyncio.CancelledError):
        await waiter
    manager.task_sem.release()
    assert manager.task_sem._value == 1
    await manager.qb.close()


@pytest.mark.asyncio
async def test_active_task_cancel_cleans_workspace(config, monkeypatch):
    manager = TaskManager(config)
    task = manager.create_task(1, 1, 1, Source(SourceType.DIRECT_URL, "https://example.com/a"), Destination.TELEGRAM, AddOptions())

    async def resolved(source): return source
    async def download(*_args, **_kwargs):
        task.work_dir.mkdir(parents=True)
        (task.work_dir / "partial").write_text("x")
        await asyncio.sleep(60)
    monkeypatch.setattr("mirrorbot.services.task_manager.resolve_source", resolved)
    monkeypatch.setattr(manager, "_download", download)
    job = asyncio.create_task(manager.run_task(task))
    await asyncio.sleep(0.05)
    assert manager.cancel(task.short_id())
    await job
    assert task.phase == TaskPhase.CANCELLED
    assert not task.work_dir.exists()
    assert manager.task_sem._value == 1
    await manager.qb.close()


@pytest.mark.asyncio
async def test_shutdown_is_idempotent_and_closes_jobs(config, monkeypatch):
    manager = TaskManager(config)
    closed = False
    async def close():
        nonlocal closed
        closed = True
    monkeypatch.setattr(manager.qb, "close", close)
    manager.spawn(asyncio.sleep(60), name="long")
    await manager.shutdown(timeout=1)
    await manager.shutdown(timeout=1)
    assert closed and not manager.runner_jobs and not manager.accepting_tasks
