from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .core.config import Config
from .services.background import BackgroundTasks
from .services.drive_search_pages import DriveSearchPages
from .services.drive_share_pages import DriveSharePages
from .services.jellyfin import JellyfinManager
from .services.jellyfin_api import JellyfinApi
from .services.task_manager import TaskManager


@dataclass
class BotContext:
    config: Config
    manager: TaskManager
    background: BackgroundTasks
    jellyfin: JellyfinManager
    jellyfin_api: JellyfinApi
    drive_search_pages: DriveSearchPages
    drive_share_pages: DriveSharePages
    telegram_app: Any
    get_file_explorer: Callable[[], Any]
