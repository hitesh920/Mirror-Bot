"""Process-wide singletons and shared state.

Importing this module builds the Pyrogram client, task manager, and background
runner exactly once. Command modules and the entrypoint both import from here,
so nothing needs to import ``mirrorbot.app`` (which would be circular).
"""

import logging

from pyrogram import Client, filters
from pyrogram.types import Message

from .core.config import Config
from .core.logging_config import setup_logging
from .services.background import BackgroundTasks
from .services.task_manager import TaskManager
from .telegram.pending import PendingAdds
from .telegram.status import TelegramStatus

setup_logging()
LOGGER = logging.getLogger("mirrorbot")

config = Config.load()
manager = TaskManager(config)
background = BackgroundTasks()

app = Client(
    "mirrorbot",
    api_id=config.telegram_api_id,
    api_hash=config.telegram_api_hash,
    bot_token=config.bot_token,
    max_concurrent_transmissions=config.task_limit,
)
telegram_status = TelegramStatus(
    app, manager, background, config.status_update_interval
)
pending_adds = PendingAdds(background)

_state = {"shutting_down": False}


def begin_shutdown() -> None:
    _state["shutting_down"] = True


def is_shutting_down() -> bool:
    return _state["shutting_down"]


def _owner_only(_, __, message: Message) -> bool:
    user = message.from_user or message.sender_chat
    return bool(not is_shutting_down() and user and user.id == config.owner_id)


owner_filter = filters.create(_owner_only)
