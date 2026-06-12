import asyncio
import shutil
from pathlib import Path
from time import monotonic

from ..core.errors import DiskSpaceError, StalledTransferError
from ..core.models import Task, TaskPhase

GIB = 1024 ** 3
MIN_RESERVE = 5 * GIB
RESERVE_RATIO = 0.05
STALL_TIMEOUT = 600
CHECK_INTERVAL = 5
STALL_PHASES = {TaskPhase.DOWNLOADING, TaskPhase.UPLOADING}


def existing_path(path: Path) -> Path:
    candidate = path.resolve(strict=False)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def disk_reserve(path: Path) -> int:
    usage = shutil.disk_usage(existing_path(path))
    return max(MIN_RESERVE, int(usage.total * RESERVE_RATIO))


def ensure_disk_space(path: Path, required: int = 0) -> None:
    usage = shutil.disk_usage(existing_path(path))
    reserve = max(MIN_RESERVE, int(usage.total * RESERVE_RATIO))
    if usage.free - max(0, required) < reserve:
        raise DiskSpaceError(
            f"Insufficient disk space: preserving {reserve / GIB:.1f} GiB free"
        )


class TransferGuard:
    def __init__(self, task: Task):
        self.task = task
        self.last_bytes = task.downloaded
        self.last_progress = task.progress
        self.last_activity = monotonic()
        self.last_phase = task.phase

    async def monitor(self) -> None:
        while not self.task.terminal:
            await asyncio.sleep(CHECK_INTERVAL)
            if self.task.cancelled:
                return
            path = self.task.guard_path or self.task.work_dir
            try:
                ensure_disk_space(path)
            except DiskSpaceError as exc:
                self.task.fail_guard(exc)
                return
            if self.task.phase != self.last_phase:
                self.last_phase = self.task.phase
                self.last_activity = monotonic()
                self.task.last_progress_at = self.last_activity
            if self.task.downloaded > self.last_bytes or self.task.progress > self.last_progress:
                self.last_bytes = self.task.downloaded
                self.last_progress = self.task.progress
                self.last_activity = monotonic()
                self.task.last_progress_at = self.last_activity
                self.task.last_processed_bytes = self.task.downloaded
            if (
                self.task.phase in STALL_PHASES
                and monotonic() - self.last_activity >= STALL_TIMEOUT
            ):
                self.task.fail_guard(
                    StalledTransferError("Transfer stalled for 10 minutes without progress")
                )
                return
