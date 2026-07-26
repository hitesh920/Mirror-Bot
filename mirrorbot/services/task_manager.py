import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

from ..core.config import Config
from ..core.logging_config import log_event
from ..core.models import SourceType, Task, TaskPhase
from ..downloaders.direct import download_direct
from ..downloaders.qbittorrent import QBittorrentClient
from ..downloaders.telegram import download_telegram_file
from ..downloaders.torrent import download_torrent
from ..downloaders.torrent_selector import TorrentSelector
from ..downloaders.ytdlp import download_ytdlp
from .public_url import public_base_url
from .task_runner import TaskRunner

LOGGER = logging.getLogger(__name__)
MAX_TERMINAL_TASKS = 200


class TaskManager:
    """Owns task registry, engine clients, queueing, and cancellation."""

    def __init__(self, config: Config):
        self.config = config
        self.tasks: dict[str, Task] = {}
        self.task_sem = asyncio.Semaphore(config.task_limit)
        self.runner_jobs: set[asyncio.Task] = set()
        self.accepting_tasks = True
        self.qb = QBittorrentClient(config.qb_host)
        self.torrent_selector = TorrentSelector(
            self.qb,
            public_base_url(config.torrent_selection_port, config.public_base_url),
            config.torrent_selection_port,
            config.torrent_selection_timeout,
        )
        self.runner = TaskRunner(self)

    def create_task(
        self, user_id, chat_id, message_id, source, destination, options
    ) -> Task:
        if not self.accepting_tasks:
            raise RuntimeError("Bot is shutting down and cannot accept new tasks")
        task_id = str(uuid4())
        task = Task(
            id=task_id,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            source=source,
            destination=destination,
            options=options,
            work_dir=self.config.download_dir / task_id,
        )
        self.tasks[task_id] = task
        log_event(
            LOGGER,
            logging.INFO,
            "task.created",
            task=task.short_id(),
            phase=task.phase.value,
            engine=source.type.value,
            destination=destination.value,
        )
        return task

    def spawn(self, awaitable, *, name: str = "task-runner") -> asyncio.Task:
        job = asyncio.create_task(awaitable, name=name)
        self.runner_jobs.add(job)
        job.add_done_callback(self.runner_jobs.discard)
        return job

    async def run_task(
        self,
        task: Task,
        telegram_reply=None,
        telegram_client=None,
        on_selector_ready=None,
        on_selector_done=None,
    ) -> Task:
        return await self.runner.run_task(
            task,
            telegram_reply=telegram_reply,
            telegram_client=telegram_client,
            on_selector_ready=on_selector_ready,
            on_selector_done=on_selector_done,
        )

    async def _download(
        self,
        task: Task,
        telegram_reply=None,
        telegram_client=None,
        on_selector_ready=None,
        on_selector_done=None,
    ) -> Path:
        if task.source.type == SourceType.TELEGRAM_FILE:
            return await download_telegram_file(task, telegram_reply, telegram_client)
        if task.source.type == SourceType.TORRENT_FILE:
            torrent_file = (
                await download_telegram_file(task, telegram_reply, telegram_client)
                if telegram_reply is not None
                else None
            )
            return await download_torrent(
                task,
                self.qb,
                self.torrent_selector,
                torrent_file=torrent_file,
                on_selector_ready=on_selector_ready,
                on_selector_done=on_selector_done,
            )
        if task.source.type == SourceType.MAGNET:
            return await download_torrent(
                task,
                self.qb,
                self.torrent_selector,
                on_selector_ready=on_selector_ready,
                on_selector_done=on_selector_done,
            )
        if task.source.type == SourceType.DIRECT_URL:
            return await download_direct(task)
        if task.source.type == SourceType.YTDLP:
            return await download_ytdlp(task)
        raise NotImplementedError(
            f"{task.source.type.value} download is not implemented"
        )

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None or task.terminal:
            return False
        if not task.request_cancel():
            return False
        LOGGER.info("Task %s: cancellation requested", task.short_id())
        return True

    async def close_active_selector(self, task_id: str = "") -> None:
        if task_id:
            task = self.get(task_id)
            if task and task.torrent_hash:
                await self.torrent_selector.cancel(task.torrent_hash)
            return
        await self.torrent_selector.cancel_all()

    def get(self, task_id_or_short: str) -> Task | None:
        if task_id_or_short in self.tasks:
            return self.tasks[task_id_or_short]
        for task in self.tasks.values():
            if task.short_id() == task_id_or_short:
                return task
        return None

    def active_tasks(self) -> list[Task]:
        return [task for task in self.tasks.values() if not task.terminal]

    def terminal_tasks(self) -> list[Task]:
        return [task for task in self.tasks.values() if task.terminal]

    def _prune_terminal_tasks(self) -> None:
        terminal = sorted(
            self.terminal_tasks(),
            key=lambda task: task.created_at,
            reverse=True,
        )
        for task in terminal[MAX_TERMINAL_TASKS:]:
            self.tasks.pop(task.id, None)

    @staticmethod
    def _record_result_manifest(task: Task, path: Path) -> None:
        task.result_name = path.name
        task.result_files = []
        task.result_folders = []
        if path.is_file():
            task.result_files = [path.name]
            return
        for item in sorted(path.rglob("*")):
            relative = item.relative_to(path).as_posix()
            task.current_file = relative
            if item.is_file():
                task.result_files.append(relative)
            elif item.is_dir():
                task.result_folders.append(relative)

    @staticmethod
    def _cleanup(path: Path) -> None:
        if path.exists():
            rmtree(path, ignore_errors=True)

    @staticmethod
    def _start_processing_phase(task: Task, phase: TaskPhase, path: Path) -> None:
        task.transition(phase, path.name)
        task.size = (
            path.stat().st_size
            if path.is_file()
            else sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
        )
        task.downloaded = 0
        task.progress = 0
        task.speed = 0
        task.eta = 0

    @staticmethod
    def _raise_if_cancelled(task: Task) -> None:
        if task.guard_error:
            raise task.guard_error
        if task.cancelled:
            raise asyncio.CancelledError()

    async def shutdown(self, timeout: int = 30) -> None:
        self.accepting_tasks = False
        for task in self.active_tasks():
            task.request_cancel("Bot shutdown")
        await self.close_active_selector()
        jobs = list(self.runner_jobs)
        if jobs:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*jobs, return_exceptions=True), timeout
                )
            except TimeoutError:
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
        await self.qb.close()

    async def cleanup_orphaned_torrents(self, attempts: int = 30) -> int:
        torrents = None
        for attempt in range(1, attempts + 1):
            try:
                torrents = await self.qb.info()
                break
            except Exception:
                if attempt >= attempts:
                    LOGGER.exception("Could not inspect qBittorrent for orphaned tasks")
                    return 0
                await asyncio.sleep(1)
        removed = 0
        for torrent in torrents or []:
            torrent_hash = str(torrent.get("hash") or "")
            if not torrent_hash:
                continue
            try:
                await self.qb.delete(torrent_hash, True)
                removed += 1
            except Exception:
                LOGGER.exception(
                    "Could not remove orphaned qBittorrent task hash=%s",
                    torrent_hash[:8],
                )
        if removed:
            LOGGER.info("Removed %s orphaned qBittorrent task(s)", removed)
        return removed

    async def _run_or_cancel(self, task: Task, awaitable):
        self._raise_if_cancelled(task)
        operation = asyncio.create_task(awaitable)
        cancelled = asyncio.create_task(task.cancel_event.wait())
        done, _ = await asyncio.wait(
            {operation, cancelled}, return_when=asyncio.FIRST_COMPLETED
        )
        if cancelled in done or task.cancelled:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            if task.guard_error:
                raise task.guard_error
            raise asyncio.CancelledError()
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        return await operation

    @asynccontextmanager
    async def _queue_slot(self, semaphore: asyncio.Semaphore, task: Task):
        self._raise_if_cancelled(task)
        acquire = asyncio.create_task(semaphore.acquire())
        cancelled = asyncio.create_task(task.cancel_event.wait())
        done, _ = await asyncio.wait(
            {acquire, cancelled}, return_when=asyncio.FIRST_COMPLETED
        )
        if cancelled in done or task.cancelled:
            if acquire.done() and not acquire.cancelled():
                semaphore.release()
            else:
                acquire.cancel()
                await asyncio.gather(acquire, return_exceptions=True)
            if not cancelled.done():
                cancelled.cancel()
                await asyncio.gather(cancelled, return_exceptions=True)
            raise asyncio.CancelledError()
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        try:
            yield
        finally:
            semaphore.release()
