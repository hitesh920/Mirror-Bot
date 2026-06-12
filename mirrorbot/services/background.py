import asyncio
import logging

LOGGER = logging.getLogger(__name__)


class BackgroundTasks:
    def __init__(self):
        self.tasks: set[asyncio.Task] = set()
        self.closing = False

    def create(self, awaitable, *, name: str = "") -> asyncio.Task:
        if self.closing:
            raise RuntimeError("Bot is shutting down")
        task = asyncio.create_task(awaitable, name=name or None)
        self.tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            exc = task.exception()
            LOGGER.error(
                "Background task failed name=%s: %s",
                task.get_name(), exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def close(self, timeout: int = 30) -> None:
        self.closing = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout)
            except TimeoutError:
                LOGGER.warning("Timed out waiting for background tasks to stop count=%s", len(tasks))
