import asyncio
import logging
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

from .archive import extract_path, zip_path
from .config import Config
from .downloaders.direct import download_direct
from .downloaders.telegram import download_telegram_file
from .downloaders.torrent import download_torrent
from .downloaders.ytdlp import download_ytdlp
from .models import Destination, SourceType, Task, TaskPhase
from .paths import deliver_to_local
from .qbittorrent import QBittorrentClient
from .resolvers import resolve_source
from .torrent_selector import TorrentSelector

LOGGER = logging.getLogger(__name__)


class TaskManager:
    def __init__(self, config: Config):
        self.config = config
        self.tasks: dict[str, Task] = {}
        self.download_sem = asyncio.Semaphore(config.queue_download_limit)
        self.upload_sem = asyncio.Semaphore(config.queue_upload_limit)
        self.qb = QBittorrentClient(config.qb_host)
        self.torrent_selector = TorrentSelector(
            self.qb,
            config.public_base_url,
            config.torrent_selection_port,
            config.torrent_selection_timeout,
        )

    def create_task(self, user_id, chat_id, message_id, source, destination, options) -> Task:
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
        LOGGER.info(
            "Task %s: created source=%s destination=%s",
            task.short_id(),
            source.type.value,
            destination.value,
        )
        return task

    async def run_local_task(
        self,
        task: Task,
        telegram_reply=None,
        on_selector_ready=None,
        on_selector_done=None,
    ) -> Task:
        try:
            async with self.download_sem:
                task.phase = TaskPhase.DOWNLOADING
                LOGGER.info("Task %s: phase=%s", task.short_id(), task.phase.value)
                task.source = await resolve_source(task.source)
                downloaded = await self._download(
                    task, telegram_reply, on_selector_ready, on_selector_done
                )

            task.phase = TaskPhase.PROCESSING
            LOGGER.info("Task %s: phase=%s path=%s", task.short_id(), task.phase.value, downloaded)
            if task.options.extract:
                LOGGER.info("Task %s: extracting archive", task.short_id())
                downloaded = await extract_path(downloaded, task.options.extract_password)
            if task.options.zip:
                LOGGER.info("Task %s: creating zip archive", task.short_id())
                downloaded = await zip_path(downloaded, task.options.zip_password)

            async with self.upload_sem:
                task.phase = TaskPhase.DELIVERING
                LOGGER.info("Task %s: phase=%s", task.short_id(), task.phase.value)
                category = "movies" if task.destination == Destination.LOCAL_MOVIES else "series"
                task.result_path = deliver_to_local(downloaded, self.config.local_download_root, category)
            task.phase = TaskPhase.COMPLETE
            LOGGER.info(
                "Task %s: complete result=%s",
                task.short_id(),
                task.result_path,
            )
        except asyncio.CancelledError:
            task.phase = TaskPhase.CANCELLED
            task.cancelled = True
            LOGGER.info("Task %s: cancelled", task.short_id())
        except Exception as exc:
            task.phase = TaskPhase.ERROR
            task.error = str(exc)
            LOGGER.exception("Task %s: failed", task.short_id())
        finally:
            if task.torrent_hash and task.phase in {
                TaskPhase.CANCELLED,
                TaskPhase.ERROR,
            }:
                try:
                    await self.qb.delete(task.torrent_hash, True)
                except Exception:
                    LOGGER.exception(
                        "Task %s: failed to clean qBittorrent task", task.short_id()
                    )
            self._cleanup(task.work_dir)
        return task

    async def _download(
        self,
        task: Task,
        telegram_reply=None,
        on_selector_ready=None,
        on_selector_done=None,
    ) -> Path:
        if task.source.type == SourceType.TELEGRAM_FILE:
            return await download_telegram_file(task, telegram_reply)
        if task.source.type == SourceType.TORRENT_FILE:
            torrent_file = (
                await download_telegram_file(task, telegram_reply)
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
        raise NotImplementedError(f"{task.source.type.value} download is planned but not implemented in this pass")

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None:
            return False
        task.cancelled = True
        LOGGER.info("Task %s: cancellation requested", task.short_id())
        return True

    def get(self, task_id_or_short: str) -> Task | None:
        if task_id_or_short in self.tasks:
            return self.tasks[task_id_or_short]
        for task in self.tasks.values():
            if task.short_id() == task_id_or_short:
                return task
        return None

    def active_tasks(self) -> list[Task]:
        return [task for task in self.tasks.values() if task.phase not in {TaskPhase.COMPLETE, TaskPhase.CANCELLED, TaskPhase.ERROR}]

    def _cleanup(self, path: Path) -> None:
        if path.exists():
            rmtree(path, ignore_errors=True)
