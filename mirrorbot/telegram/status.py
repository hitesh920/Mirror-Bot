import asyncio
import logging
from collections import defaultdict

from pyrogram.enums import ParseMode

from ..services.status import format_status

LOGGER = logging.getLogger(__name__)


class TelegramStatus:
    def __init__(self, app, manager, background, interval: int):
        self.app = app
        self.manager = manager
        self.background = background
        self.interval = interval
        self.messages = {}
        self.jobs: dict[int, asyncio.Task] = {}
        self.text: dict[int, str] = {}
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def chat_tasks(self, chat_id: int):
        return [
            task
            for task in self.manager.active_tasks()
            if task.chat_id == chat_id and task.status_visible
        ]

    async def update(self, chat_id: int) -> None:
        async with self.locks[chat_id]:
            tasks = self.chat_tasks(chat_id)
            if not tasks:
                message = self.messages.pop(chat_id, None)
                self.text.pop(chat_id, None)
                if message:
                    try:
                        await message.delete()
                    except Exception:
                        LOGGER.debug(
                            "Could not delete completed status chat=%s",
                            chat_id,
                            exc_info=True,
                        )
                return

            text = format_status(tasks)
            message = self.messages.get(chat_id)
            if message is None:
                self.messages[chat_id] = await self.app.send_message(
                    chat_id,
                    text,
                    parse_mode=ParseMode.HTML,
                    disable_notification=True,
                )
                self.text[chat_id] = text
            elif self.text.get(chat_id) != text:
                try:
                    await message.edit_text(text, parse_mode=ParseMode.HTML)
                    self.text[chat_id] = text
                except Exception:
                    LOGGER.exception(
                        "Could not update status message chat=%s", chat_id
                    )

    async def replace(self, chat_id: int) -> None:
        async with self.locks[chat_id]:
            text = format_status(self.chat_tasks(chat_id))
            new_message = await self.app.send_message(
                chat_id,
                text,
                parse_mode=ParseMode.HTML,
                disable_notification=True,
            )
            old_message = self.messages.get(chat_id)
            self.messages[chat_id] = new_message
            self.text[chat_id] = text
            if old_message:
                try:
                    await old_message.delete()
                except Exception:
                    LOGGER.debug(
                        "Could not delete replaced status chat=%s",
                        chat_id,
                        exc_info=True,
                    )
        self.ensure_loop(chat_id)

    async def start(self, chat_id: int, message) -> None:
        async with self.locks[chat_id]:
            old_message = self.messages.get(chat_id)
            text = format_status(self.chat_tasks(chat_id))
            await message.edit_text(text, parse_mode=ParseMode.HTML)
            self.messages[chat_id] = message
            self.text[chat_id] = text
            if old_message and old_message.id != message.id:
                try:
                    await old_message.delete()
                except Exception:
                    LOGGER.debug(
                        "Could not delete old status chat=%s",
                        chat_id,
                        exc_info=True,
                    )
        self.ensure_loop(chat_id)

    async def send(self, chat_id: int) -> None:
        await self.update(chat_id)
        self.ensure_loop(chat_id)

    def ensure_loop(self, chat_id: int) -> None:
        job = self.jobs.get(chat_id)
        if job is None or job.done():
            self.jobs[chat_id] = self.background.create(
                self._loop(chat_id), name="status-loop"
            )

    async def _loop(self, chat_id: int) -> None:
        try:
            while self.chat_tasks(chat_id):
                await self.update(chat_id)
                await asyncio.sleep(self.interval)
            await self.update(chat_id)
        finally:
            self.jobs.pop(chat_id, None)

    def cancel_jobs(self) -> None:
        for job in list(self.jobs.values()):
            job.cancel()
