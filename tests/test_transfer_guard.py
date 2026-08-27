"""Coverage for the disk/stall watchdog that can force-kill tasks (#29)."""

import pytest

from mirrorbot.core.errors import DiskSpaceError, StalledTransferError
from mirrorbot.core.models import TaskPhase
from mirrorbot.services import transfer_guard
from mirrorbot.services.transfer_guard import TransferGuard


@pytest.fixture(autouse=True)
def _fast_guard(monkeypatch, clock):
    """Drive monitor() on a fake clock with an instant sleep."""
    monkeypatch.setattr(transfer_guard, "monotonic", clock)
    monkeypatch.setattr(transfer_guard, "CHECK_INTERVAL", 5)
    monkeypatch.setattr(transfer_guard, "STALL_TIMEOUT", 600)

    async def _sleep(_seconds):
        clock.advance(transfer_guard.CHECK_INTERVAL)

    monkeypatch.setattr(transfer_guard.asyncio, "sleep", _sleep)
    return clock


def _allow_disk(monkeypatch):
    monkeypatch.setattr(transfer_guard, "ensure_disk_space", lambda *a, **k: None)


async def test_guard_flags_low_disk(make_task, monkeypatch):
    task = make_task(phase=TaskPhase.DOWNLOADING)

    def _boom(*_a, **_k):
        raise DiskSpaceError("no space")

    monkeypatch.setattr(transfer_guard, "ensure_disk_space", _boom)

    await TransferGuard(task).monitor()

    assert isinstance(task.guard_error, DiskSpaceError)
    assert task.cancelled is True
    assert task.failure_category == "disk"


async def test_guard_flags_stall_after_timeout(make_task, monkeypatch, _fast_guard):
    _allow_disk(monkeypatch)
    task = make_task(phase=TaskPhase.DOWNLOADING)
    # Guard captured "now" at construction; keep phase steady and never progress.
    guard = TransferGuard(task)

    await guard.monitor()

    assert isinstance(task.guard_error, StalledTransferError)
    assert task.failure_category == "stalled"


async def test_guard_does_not_flag_while_progressing(
    make_task, monkeypatch, _fast_guard
):
    _allow_disk(monkeypatch)
    task = make_task(phase=TaskPhase.DOWNLOADING)
    guard = TransferGuard(task)

    ticks = {"n": 0}
    real_sleep = transfer_guard.asyncio.sleep

    async def _sleep(seconds):
        await real_sleep(seconds)
        ticks["n"] += 1
        task.downloaded += 1_000_000
        if ticks["n"] >= 400:  # well past STALL_TIMEOUT / CHECK_INTERVAL
            task.transition(TaskPhase.COMPLETE)

    monkeypatch.setattr(transfer_guard.asyncio, "sleep", _sleep)

    await guard.monitor()

    assert task.guard_error is None


async def test_guard_ignores_stall_outside_stall_phases(
    make_task, monkeypatch, _fast_guard
):
    _allow_disk(monkeypatch)
    task = make_task(phase=TaskPhase.EXTRACTING)
    guard = TransferGuard(task)

    ticks = {"n": 0}
    real_sleep = transfer_guard.asyncio.sleep

    async def _sleep(seconds):
        await real_sleep(seconds)
        ticks["n"] += 1
        if ticks["n"] >= 400:
            task.transition(TaskPhase.COMPLETE)

    monkeypatch.setattr(transfer_guard.asyncio, "sleep", _sleep)

    await guard.monitor()

    assert task.guard_error is None


async def test_guard_returns_immediately_when_cancelled(make_task, monkeypatch):
    _allow_disk(monkeypatch)
    task = make_task(phase=TaskPhase.DOWNLOADING)
    task.cancelled = True

    await TransferGuard(task).monitor()

    assert task.guard_error is None
