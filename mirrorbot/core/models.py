import asyncio
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from time import monotonic, time
from typing import Any


class SourceType(str, Enum):
    DIRECT_URL = "direct_url"
    TELEGRAM_FILE = "telegram_file"
    MAGNET = "magnet"
    TORRENT_FILE = "torrent_file"
    YTDLP = "ytdlp"
    GOOGLE_DRIVE = "google_drive"
    LOCAL_PATH = "local_path"
    UNSUPPORTED = "unsupported"


class Destination(str, Enum):
    LOCAL_MOVIES = "local_movies"
    LOCAL_SERIES = "local_series"
    TELEGRAM = "telegram"
    GOOGLE_DRIVE = "google_drive"


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
    MOVING = "moving"
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

    @property
    def terminal(self) -> bool:
        return self.phase in {TaskPhase.COMPLETE, TaskPhase.CANCELLED, TaskPhase.ERROR}

    def transition(self, phase: TaskPhase, current_file: str = "") -> None:
        if self.terminal and phase != self.phase:
            return
        self.phase = phase
        if current_file:
            self.current_file = current_file
        self.last_progress_at = monotonic()

    def request_cancel(self, reason: str = "Cancelled by user") -> bool:
        if self.terminal or self.cancelled:
            return False
        self.cancelled = True
        self.cancel_reason = reason
        self.cancel_event.set()
        return True

    def fail_guard(self, error: Exception) -> None:
        if self.terminal or self.guard_error is not None:
            return
        self.guard_error = error
        self.failure_category = getattr(error, "category", "engine")
        self.cancelled = True
        self.cancel_reason = str(error)
        self.cancel_event.set()

    def short_id(self) -> str:
        return self.id.split("-", 1)[0]
