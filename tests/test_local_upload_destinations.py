from pathlib import Path

import pytest

from mirrorbot.core.models import AddOptions, Destination, Source, SourceType, TaskPhase
from mirrorbot.services.file_explorer import PAGE
from mirrorbot.services.task_manager import TaskManager


def test_file_explorer_offers_both_upload_destinations():
    assert "Upload to Telegram" in PAGE
    assert "Upload to Google Drive" in PAGE
    assert "upload('telegram')" in PAGE
    assert "upload('google_drive')" in PAGE


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", [Destination.TELEGRAM, Destination.GOOGLE_DRIVE])
async def test_local_upload_uses_selected_destination(config, monkeypatch, destination):
    path = config.local_download_root / "file.bin"
    path.write_bytes(b"test")
    manager = TaskManager(config)
    task = manager.create_task(
        1,
        1,
        0,
        Source(SourceType.LOCAL_PATH, str(path), path.name),
        destination,
        AddOptions(),
    )
    calls = []

    async def telegram(*_):
        calls.append(Destination.TELEGRAM)

    async def drive(*_):
        calls.append(Destination.GOOGLE_DRIVE)

    monkeypatch.setattr("mirrorbot.services.task_manager.upload_to_telegram", telegram)
    monkeypatch.setattr("mirrorbot.services.task_manager.upload_to_gdrive", drive)

    await manager.run_local_upload(task, path, object())

    assert task.phase == TaskPhase.COMPLETE
    assert calls == [destination]
    await manager.qb.close()
