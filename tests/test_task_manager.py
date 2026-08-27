"""Coverage for the unified race-against-cancel primitive (#14)."""

import asyncio
from types import SimpleNamespace

import pytest

from mirrorbot.core.errors import DiskSpaceError
from mirrorbot.services.task_manager import TaskManager


@pytest.fixture
def manager(tmp_path):
    config = SimpleNamespace(
        task_limit=2,
        qb_host="http://qb:8080",
        torrent_selection_port=8001,
        torrent_selection_timeout=300,
        public_base_url="http://test:8001",
        download_dir=tmp_path / "downloads",
    )
    return TaskManager(config)


async def test_run_or_cancel_returns_result(manager, make_task):
    task = make_task()

    async def work():
        await asyncio.sleep(0)
        return "value"

    assert await manager._run_or_cancel(task, work()) == "value"


async def test_run_or_cancel_propagates_operation_error(manager, make_task):
    task = make_task()

    async def work():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await manager._run_or_cancel(task, work())


async def test_run_or_cancel_raises_when_cancelled_midway(manager, make_task):
    task = make_task()
    started = asyncio.Event()

    async def work():
        started.set()
        await asyncio.sleep(10)

    op = asyncio.create_task(manager._run_or_cancel(task, work()))
    await started.wait()
    task.request_cancel("stop")

    with pytest.raises(asyncio.CancelledError):
        await op


async def test_run_or_cancel_raises_guard_error(manager, make_task):
    task = make_task()
    started = asyncio.Event()

    async def work():
        started.set()
        await asyncio.sleep(10)

    op = asyncio.create_task(manager._run_or_cancel(task, work()))
    await started.wait()
    task.fail_guard(DiskSpaceError("disk full"))

    with pytest.raises(DiskSpaceError):
        await op


async def test_queue_slot_releases_semaphore_on_exit(manager, make_task):
    task = make_task()
    sem = asyncio.Semaphore(1)

    async with manager._queue_slot(sem, task):
        assert sem.locked()
    assert not sem.locked()


async def test_queue_slot_releases_semaphore_when_cancelled(manager, make_task):
    task = make_task()
    sem = asyncio.Semaphore(1)
    await sem.acquire()  # slot is taken; acquire() inside will block

    slot = asyncio.create_task(_enter(manager, sem, task))
    await asyncio.sleep(0)
    task.request_cancel("stop")
    with pytest.raises(asyncio.CancelledError):
        await slot

    sem.release()
    assert sem._value == 1  # nothing leaked


async def _enter(manager, sem, task):
    async with manager._queue_slot(sem, task):
        pass
