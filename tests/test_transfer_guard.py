import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from mirrorbot.core.errors import DiskSpaceError, StalledTransferError
from mirrorbot.core.models import AddOptions, Destination, Source, SourceType, Task, TaskPhase
from mirrorbot.services import transfer_guard


def task(tmp_path: Path, phase=TaskPhase.DOWNLOADING):
    return Task("guard-id", 1, 1, 1, Source(SourceType.DIRECT_URL, "x"), Destination.TELEGRAM, AddOptions(), tmp_path, phase=phase)


def test_dynamic_disk_reserve(monkeypatch, tmp_path):
    monkeypatch.setattr(transfer_guard.shutil, "disk_usage", lambda _p: SimpleNamespace(total=100 * transfer_guard.GIB, used=94 * transfer_guard.GIB, free=6 * transfer_guard.GIB))
    assert transfer_guard.disk_reserve(tmp_path) == 5 * transfer_guard.GIB
    transfer_guard.ensure_disk_space(tmp_path, transfer_guard.GIB)
    with pytest.raises(DiskSpaceError):
        transfer_guard.ensure_disk_space(tmp_path, transfer_guard.GIB + 1)

    monkeypatch.setattr(transfer_guard.shutil, "disk_usage", lambda _p: SimpleNamespace(total=200 * transfer_guard.GIB, used=189 * transfer_guard.GIB, free=11 * transfer_guard.GIB))
    assert transfer_guard.disk_reserve(tmp_path) == 10 * transfer_guard.GIB
    with pytest.raises(DiskSpaceError):
        transfer_guard.ensure_disk_space(tmp_path, 2 * transfer_guard.GIB)


@pytest.mark.asyncio
async def test_disk_exhaustion_cancels_transfer(monkeypatch, tmp_path):
    monkeypatch.setattr(transfer_guard, "CHECK_INTERVAL", 0.01)
    monkeypatch.setattr(transfer_guard, "ensure_disk_space", lambda *_a, **_k: (_ for _ in ()).throw(DiskSpaceError("reserve reached")))
    item = task(tmp_path)
    await transfer_guard.TransferGuard(item).monitor()
    assert isinstance(item.guard_error, DiskSpaceError)
    assert item.cancelled


@pytest.mark.asyncio
async def test_stalled_transfer_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(transfer_guard, "CHECK_INTERVAL", 0.01)
    monkeypatch.setattr(transfer_guard, "STALL_TIMEOUT", 0.03)
    monkeypatch.setattr(transfer_guard, "ensure_disk_space", lambda *_a, **_k: None)
    item = task(tmp_path)
    await transfer_guard.TransferGuard(item).monitor()
    assert isinstance(item.guard_error, StalledTransferError)
    assert item.cancelled


@pytest.mark.asyncio
async def test_phase_change_resets_stall_clock(monkeypatch, tmp_path):
    monkeypatch.setattr(transfer_guard, "CHECK_INTERVAL", 0.01)
    monkeypatch.setattr(transfer_guard, "STALL_TIMEOUT", 0.03)
    monkeypatch.setattr(transfer_guard, "ensure_disk_space", lambda *_a, **_k: None)
    item = task(tmp_path, TaskPhase.METADATA)
    monitor = asyncio.create_task(transfer_guard.TransferGuard(item).monitor())
    await asyncio.sleep(0.05)
    item.transition(TaskPhase.DOWNLOADING)
    await asyncio.sleep(0.02)
    item.transition(TaskPhase.COMPLETE)
    await monitor
    assert item.guard_error is None


@pytest.mark.asyncio
async def test_progress_prevents_false_stall(monkeypatch, tmp_path):
    monkeypatch.setattr(transfer_guard, "CHECK_INTERVAL", 0.01)
    monkeypatch.setattr(transfer_guard, "STALL_TIMEOUT", 0.05)
    monkeypatch.setattr(transfer_guard, "ensure_disk_space", lambda *_a, **_k: None)
    item = task(tmp_path)
    monitor = asyncio.create_task(transfer_guard.TransferGuard(item).monitor())
    for value in range(1, 8):
        await asyncio.sleep(0.01)
        item.downloaded = value
    item.transition(TaskPhase.COMPLETE)
    await monitor
    assert item.guard_error is None