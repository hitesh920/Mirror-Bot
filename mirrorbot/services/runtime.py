import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable

from .background import BackgroundTasks
from .task_manager import TaskManager

LOGGER = logging.getLogger(__name__)
Cleanup = Callable[[], Awaitable[None]]


class RuntimeCoordinator:
    def __init__(self, manager: TaskManager, background: BackgroundTasks, timeout: int = 30):
        self.manager = manager
        self.background = background
        self.timeout = timeout
        self.closing = False

    async def shutdown(self, cleanups: Iterable[Cleanup] = ()) -> None:
        if self.closing:
            return
        self.closing = True
        LOGGER.info("Runtime shutdown started")
        await self.manager.shutdown(self.timeout)
        for cleanup in cleanups:
            try:
                await cleanup()
            except Exception:
                LOGGER.exception("Runtime service cleanup failed")
        await self.background.close(self.timeout)
        LOGGER.info("Runtime shutdown complete")