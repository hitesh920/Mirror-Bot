import asyncio
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from time import time
from typing import Any


class SourceType(str, Enum):
    DIRECT_URL = "direct_url"
    TELEGRAM_FILE = "telegram_file"
    MAGNET = "magnet"
    TORRENT_FILE = "torrent_file"
    YTDLP = "ytdlp"
    GOOGLE_DRIVE = "google_drive"
    RCLONE = "rclone"
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
    DELIVERING = "delivering"
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
    progress: float = 0
    size: int = 0
    downloaded: int = 0
    speed: int = 0
    eta: int = 0
    error: str = ""
    result_path: Path | None = None
    torrent_hash: str = ""
    selection_url: str = ""
    created_at: float = field(default_factory=time)
    cancelled: bool = False
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def short_id(self) -> str:
        return self.id.split("-", 1)[0]
