import asyncio
import shutil
from pathlib import Path
from time import monotonic

from ..core.errors import DiskSpaceError, StalledTransferError
from ..core.formatting import human_size
from ..core.models import Task, TaskPhase

GIB = 1024**3
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


def ensure_disk_space(path: Path, required: int = 0) -> None:
    usage = shutil.disk_usage(existing_path(path))
    reserve = max(MIN_RESERVE, int(usage.total * RESERVE_RATIO))
    if usage.free - max(0, required) < reserve:
        raise DiskSpaceError(
            f"Insufficient disk space: preserving {human_size(reserve)} free"
        )


class DiskReservationPool:
    """Atomically reserve remaining known-size writes across active tasks."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._remaining: dict[str, int] = {}

    async def reserve(self, task_id: str, path: Path, required: int) -> None:
        required = max(0, int(required))
        async with self._lock:
            usage = await asyncio.to_thread(shutil.disk_usage, existing_path(path))
            reserve = max(MIN_RESERVE, int(usage.total * RESERVE_RATIO))
            other_reserved = sum(
                amount for owner, amount in self._remaining.items() if owner != task_id
            )
            if usage.free - other_reserved - required < reserve:
                raise DiskSpaceError(
                    f"Insufficient disk space: preserving {human_size(reserve)} free"
                )
            if required:
                self._remaining[task_id] = required
            else:
                self._remaining.pop(task_id, None)

    async def release(self, task_id: str) -> None:
        async with self._lock:
            self._remaining.pop(task_id, None)

    async def update_remaining(self, task_id: str, path: Path, remaining: int) -> None:
        remaining = max(0, int(remaining))
        async with self._lock:
            current = self._remaining.get(task_id, 0)
            if remaining <= current:
                if remaining:
                    self._remaining[task_id] = remaining
                else:
                    self._remaining.pop(task_id, None)
                return
        await self.reserve(task_id, path, remaining)

    async def reserved(self, task_id: str = "") -> int:
        async with self._lock:
            if task_id:
                return self._remaining.get(task_id, 0)
            return sum(self._remaining.values())


async def reserve_disk_space(task: Task, path: Path, required: int) -> None:
    pool = task.disk_reservation_pool
    if pool is None:
        await asyncio.to_thread(ensure_disk_space, path, required)
        return
    await pool.reserve(task.id, path, required)


async def update_disk_reservation(task: Task, remaining: int) -> None:
    pool = task.disk_reservation_pool
    if pool is not None:
        await pool.update_remaining(
            task.id,
            task.guard_path or task.work_dir,
            remaining,
        )


async def release_disk_reservation(task: Task) -> None:
    pool = task.disk_reservation_pool
    if pool is not None:
        await pool.release(task.id)


class TransferGuard:
    def __init__(self, task: Task):
        self.task = task
        self.last_bytes = task.downloaded
        self.last_progress = task.progress
        self.last_activity = monotonic()
        self.last_phase = task.phase

    def check_progress(self, now: float | None = None) -> bool:
        current_time = monotonic() if now is None else now
        if self.task.phase != self.last_phase:
            self.last_phase = self.task.phase
            self.last_activity = current_time
            self.last_bytes = self.task.downloaded
            self.last_progress = self.task.progress
            self.task.last_progress_at = current_time
        if (
            self.task.downloaded > self.last_bytes
            or self.task.progress > self.last_progress
        ):
            self.last_bytes = self.task.downloaded
            self.last_progress = self.task.progress
            self.last_activity = current_time
            self.task.last_progress_at = current_time
            self.task.last_processed_bytes = self.task.downloaded
        if (
            self.task.phase in STALL_PHASES
            and current_time - self.last_activity >= STALL_TIMEOUT
        ):
            self.task.fail_guard(
                StalledTransferError("Transfer stalled for 10 minutes without progress")
            )
            return True
        return False

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
            if self.check_progress():
                return
