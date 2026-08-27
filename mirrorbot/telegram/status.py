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

    async def _delete_quietly(self, message, chat_id: int, reason: str) -> None:
        if message is None:
            return
        try:
            await message.delete()
        except Exception:
            LOGGER.debug("Could not delete %s status chat=%s", reason, chat_id,
                         exc_info=True)

    async def _store(self, chat_id: int, message, text: str, *, replaced_reason: str):
        """Adopt ``message`` as the live status message, deleting the previous one."""
        old = self.messages.get(chat_id)
        self.messages[chat_id] = message
        self.text[chat_id] = text
        if old is not None and old.id != message.id:
            await self._delete_quietly(old, chat_id, replaced_reason)

    async def _send_status(self, chat_id: int, text: str):
        return await self.app.send_message(
            chat_id, text, parse_mode=ParseMode.HTML, disable_notification=True
        )

    async def update(self, chat_id: int) -> None:
        idle = False
        async with self.locks[chat_id]:
            tasks = self.chat_tasks(chat_id)
            if not tasks:
                message = self.messages.pop(chat_id, None)
                self.text.pop(chat_id, None)
                await self._delete_quietly(message, chat_id, "completed")
                idle = True
            else:
                text = format_status(tasks)
                await self._update_locked(chat_id, text)
        if idle:
            self._forget(chat_id)

    async def _update_locked(self, chat_id: int, text: str) -> None:
        message = self.messages.get(chat_id)
        if message is None:
            await self._store(
                chat_id, await self._send_status(chat_id, text), text,
                replaced_reason="replaced",
            )
        elif self.text.get(chat_id) != text:
            try:
                await message.edit_text(text, parse_mode=ParseMode.HTML)
                self.text[chat_id] = text
            except Exception:
                LOGGER.exception("Could not update status message chat=%s", chat_id)

    async def replace(self, chat_id: int) -> None:
        async with self.locks[chat_id]:
            text = format_status(self.chat_tasks(chat_id))
            await self._store(
                chat_id, await self._send_status(chat_id, text), text,
                replaced_reason="replaced",
            )
        self.ensure_loop(chat_id)

    async def start(self, chat_id: int, message) -> None:
        async with self.locks[chat_id]:
            text = format_status(self.chat_tasks(chat_id))
            await message.edit_text(text, parse_mode=ParseMode.HTML)
            await self._store(chat_id, message, text, replaced_reason="old")
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

    def _forget(self, chat_id: int) -> None:
        """Drop per-chat bookkeeping once a chat has no visible tasks.

        Called after the chat's lock is released, so a now-idle lock is safe to
        remove; a still-running loop keeps its own jobs entry.
        """
        job = self.jobs.get(chat_id)
        if job is None or job.done():
            self.jobs.pop(chat_id, None)
        lock = self.locks.get(chat_id)
        if lock is not None and not lock.locked():
            self.locks.pop(chat_id, None)

    def cancel_jobs(self) -> None:
        for job in list(self.jobs.values()):
            job.cancel()
