import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from time import monotonic, time
from typing import Any

from .logging_config import log_event

LOGGER = logging.getLogger(__name__)


class SourceType(str, Enum):
    BATCH = "batch"
    DIRECT_URL = "direct_url"
    TELEGRAM_FILE = "telegram_file"
    MAGNET = "magnet"
    TORRENT_FILE = "torrent_file"
    YTDLP = "ytdlp"
    UNSUPPORTED = "unsupported"


class Destination(str, Enum):
    TELEGRAM = "telegram"
    CLOUDFLARE_R2 = "cloudflare_r2"


class TaskPhase(str, Enum):
    QUEUED = "queued"
    METADATA = "fetching metadata"
    SELECTING = "selecting"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    PREPARING = "preparing"
    SCANNING = "scanning"
    EXTRACTING = "extracting"
    ARCHIVING = "archiving"
    SPLITTING = "splitting"
    DELIVERING = "delivering"
    UPLOADING = "uploading"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class AddOptions:
    name: str = ""
    zip: bool = False
    zip_password: str = ""
    extract: bool = False
    extract_password: str = ""
    ytdlp_kind: str = ""
    ytdlp_quality: str = ""
    batch_messages: int = 0


@dataclass
class Source:
    type: SourceType
    value: str
    filename: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Task:
    id: str
    user_id: int
    chat_id: int
    message_id: int
    source: Source
    destination: Destination
    options: AddOptions
    work_dir: Path
    phase: TaskPhase = TaskPhase.QUEUED
    name: str = ""
    current_file: str = ""
    progress: float = 0
    size: int = 0
    downloaded: int = 0
    speed: int = 0
    eta: int = 0
    error: str = ""
    result_path: Path | None = None
    result_name: str = ""
    result_files: list[str] = field(default_factory=list)
    result_folders: list[str] = field(default_factory=list)
    result_links: list[str] = field(default_factory=list)
    result_auto_delete_seconds: int = 0
    result_is_folder: bool = False
    telegram_upload_mode: str = ""
    processing_warnings: list[str] = field(default_factory=list)
    batch_total: int = 0
    batch_completed: int = 0
    batch_failed: int = 0
    batch_initial_skipped: int = 0
    torrent_hash: str = ""
    selection_url: str = ""
    created_at: float = field(default_factory=time)
    status_visible: bool = True
    cancelled: bool = False
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    cancel_reason: str = ""
    failure_category: str = ""
    guard_error: Exception | None = field(default=None, repr=False)
    guard_path: Path | None = None
    last_progress_at: float = field(default_factory=monotonic)
    last_processed_bytes: int = 0
    _progress_started: float = field(default_factory=monotonic, repr=False)

    @property
    def terminal(self) -> bool:
        return self.phase in {TaskPhase.COMPLETE, TaskPhase.CANCELLED, TaskPhase.ERROR}

    def begin_progress(self, size: int = 0) -> None:
        """Reset the progress counters and start a fresh timing segment.

        Call this at the start of every transfer stage (download, extract,
        archive, split, upload) so speed/ETA are measured per stage.
        """
        self._progress_started = monotonic()
        self.size = max(0, int(size))
        self.downloaded = 0
        self.progress = 0.0
        self.speed = 0
        self.eta = 0

    def report_progress(
        self,
        downloaded: int,
        *,
        size: int | None = None,
        current_file: str | None = None,
        complete: bool = False,
    ) -> None:
        """Atomically update downloaded/size/speed/eta/progress from a byte count.

        A single synchronous call with no ``await`` inside, so concurrent
        readers (the transfer guard, the status loop) never observe a
        half-updated set of fields.
        """
        if size is not None:
            self.size = max(0, int(size))
        if current_file is not None:
            self.current_file = current_file
        if complete and not self.size:
            self.size = max(0, int(downloaded))
        if complete and self.size:
            self.downloaded = self.size
        else:
            self.downloaded = max(0, int(downloaded))
        elapsed = monotonic() - self._progress_started
        self.speed = int(self.downloaded / elapsed) if elapsed > 0 else 0
        if self.size:
            self.progress = min(self.downloaded / self.size, 1.0)
            self.eta = (
                int((self.size - self.downloaded) / self.speed) if self.speed else 0
            )
        else:
            self.progress = 1.0 if complete else 0.0
            self.eta = 0

    def advance_progress(self, delta: int) -> None:
        """Add ``delta`` bytes to the running total (see report_progress)."""
        self.report_progress(self.downloaded + delta)

    def set_transfer_stats(
        self,
        *,
        downloaded: int,
        size: int,
        speed: int,
        eta: int,
        progress: float | None = None,
    ) -> None:
        """Atomically store stats an engine reports directly (torrent, yt-dlp)."""
        self.downloaded = max(0, int(downloaded))
        self.size = max(0, int(size))
        self.speed = max(0, int(speed))
        self.eta = max(0, int(eta))
        if progress is not None:
            self.progress = min(max(float(progress), 0.0), 1.0)
        elif self.size:
            self.progress = min(self.downloaded / self.size, 1.0)

    def transition(self, phase: TaskPhase, current_file: str = "") -> None:
        if self.terminal and phase != self.phase:
            return
        previous = self.phase
        self.phase = phase
        if current_file:
            self.current_file = current_file
        self.last_progress_at = monotonic()
        if previous != phase:
            log_event(
                LOGGER,
                logging.INFO,
                "task.phase_changed",
                task=self.short_id(),
                phase=phase.value,
                previous_phase=previous.value,
                engine=self.source.type.value,
                destination=self.destination.value,
            )

    def request_cancel(self, reason: str = "Cancelled by user") -> bool:
        if self.terminal or self.cancelled:
            return False
        self.cancelled = True
        self.cancel_reason = reason
        self.cancel_event.set()
        log_event(
            LOGGER,
            logging.INFO,
            "task.cancel_requested",
            task=self.short_id(),
            phase=self.phase.value,
            reason=reason,
        )
        return True

    def fail_guard(self, error: Exception) -> None:
        if self.terminal or self.guard_error is not None:
            return
        self.guard_error = error
        self.failure_category = getattr(error, "category", "engine")
        self.cancelled = True
        self.cancel_reason = str(error)
        self.cancel_event.set()
        log_event(
            LOGGER,
            logging.WARNING,
            "task.guard_failed",
            task=self.short_id(),
            phase=self.phase.value,
            error_category=self.failure_category,
            result=error,
        )

    def short_id(self) -> str:
        return self.id.split("-", 1)[0]
