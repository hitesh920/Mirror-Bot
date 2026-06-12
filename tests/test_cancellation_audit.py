import asyncio

import pytest

from mirrorbot.core.models import AddOptions, Destination, Source, SourceType, Task, TaskPhase
from mirrorbot.services.archive import _run


@pytest.mark.parametrize("phase", [TaskPhase.METADATA, TaskPhase.SELECTING, TaskPhase.DOWNLOADING, TaskPhase.EXTRACTING, TaskPhase.ARCHIVING, TaskPhase.SPLITTING, TaskPhase.SCANNING, TaskPhase.MOVING, TaskPhase.UPLOADING])
def test_every_active_phase_accepts_cancellation(tmp_path, phase):
    task = Task("phase-id", 1, 1, 1, Source(SourceType.DIRECT_URL, "x"), Destination.TELEGRAM, AddOptions(), tmp_path, phase=phase)
    assert task.request_cancel("audit")
    assert task.cancelled and task.cancel_event.is_set()


@pytest.mark.asyncio
async def test_archive_subprocess_is_terminated(tmp_path):
    task = Task("process-id", 1, 1, 1, Source(SourceType.DIRECT_URL, "x"), Destination.TELEGRAM, AddOptions(), tmp_path, phase=TaskPhase.ARCHIVING)
    job = asyncio.create_task(_run(task, "sh", "-c", "sleep 60"))
    await asyncio.sleep(0.1)
    task.request_cancel("audit")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job, 2)
