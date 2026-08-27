"""Pending /add selections awaiting a destination / mode choice."""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass

from pyrogram.types import Message

from ..core.models import AddOptions, Source

LOGGER = logging.getLogger(__name__)
PENDING_ADD_TIMEOUT = 120


@dataclass
class PendingAdd:
    sources: list[Source]
    options: AddOptions
    reply: Message | None = None
    batch_mode: str = ""
    skipped: int = 0


class PendingAdds:
    """Token-keyed store of pending /add selections with a timed expiry."""

    def __init__(self, background):
        self._background = background
        self._pending: dict[str, PendingAdd] = {}
        self._messages: dict[str, Message] = {}
        self._jobs: dict[str, asyncio.Task] = {}

    def __setitem__(self, token: str, pending: PendingAdd) -> None:
        self._pending[token] = pending

    def __contains__(self, token: str) -> bool:
        return token in self._pending

    def get(self, token: str) -> PendingAdd | None:
        return self._pending.get(token)

    def take(self, token: str) -> PendingAdd | None:
        """Remove and return the pending selection, cancelling its expiry."""
        pending = self._pending.pop(token, None)
        self._messages.pop(token, None)
        job = self._jobs.pop(token, None)
        if job:
            job.cancel()
        return pending

    def track_expiry(self, token: str, message: Message) -> None:
        self._messages[token] = message
        old = self._jobs.pop(token, None)
        if old:
            old.cancel()
        self._jobs[token] = self._background.create(
            self._expire(token), name="expire-add"
        )

    def cancel_all(self) -> None:
        for job in list(self._jobs.values()):
            job.cancel()

    async def _expire(self, token: str) -> None:
        try:
            await asyncio.sleep(PENDING_ADD_TIMEOUT)
            pending = self._pending.pop(token, None)
            message = self._messages.pop(token, None)
            if pending is None:
                return
            LOGGER.info("Expired pending /add selection message_id=%s", token)
            if message:
                with suppress(Exception):
                    await message.edit("Selection expired. Send /add again.")
        finally:
            self._jobs.pop(token, None)
