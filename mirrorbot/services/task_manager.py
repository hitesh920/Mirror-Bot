import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

from .archive import (
    ArchiveCorruptError,
    ArchivePasswordError,
    ArchiveUnsupportedError,
    extract_path,
    zip_path,
)
from ..core.config import Config
from ..core.models import Destination, SourceType, Task, TaskPhase
from ..downloaders.direct import download_direct
from ..downloaders.gdrive import download_gdrive
from ..downloaders.qbittorrent import QBittorrentClient
from ..downloaders.rclone import download_rclone
from ..downloaders.telegram import download_telegram_file
from ..downloaders.torrent import DuplicateTorrentError, download_torrent
from ..downloaders.torrent_selector import TorrentSelector
from ..downloaders.ytdlp import download_ytdlp
from ..resolvers import resolve_source
from .local_delivery import deliver_to_local
from .telegram_delivery import upload_to_telegram

LOGGER = logging.getLogger(__name__)


class TaskManager:
    def __init__(self, config: Config):
        self.config = config
        self.tasks: dict[str, Task] = {}
        self.task_sem = asyncio.Semaphore(config.task_limit)
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

    async def run_task(
        self,
        task: Task,
        telegram_reply=None,
        telegram_client=None,
        on_selector_ready=None,
        on_selector_done=None,
    ) -> Task:
        try:
            async with self._queue_slot(self.task_sem, task):
                self._raise_if_cancelled(task)
                task.phase = TaskPhase.DOWNLOADING
                LOGGER.info("Task %s: phase=%s", task.short_id(), task.phase.value)
                task.source = await self._run_or_cancel(task, resolve_source(task.source))
                self._raise_if_cancelled(task)
                downloaded = await self._run_or_cancel(
                    task,
                    self._download(
                        task,
                        telegram_reply,
                        telegram_client,
                        on_selector_ready,
                        on_selector_done,
                    ),
                )

                self._raise_if_cancelled(task)
                task.phase = TaskPhase.PROCESSING
                LOGGER.info("Task %s: phase=%s path=%s", task.short_id(), task.phase.value, downloaded)
                if task.options.extract:
                    LOGGER.info("Task %s: extracting archive", task.short_id())
                    downloaded = await extract_path(
                        downloaded, task, task.options.extract_password
                    )
                    self._raise_if_cancelled(task)
                if task.options.zip:
                    LOGGER.info("Task %s: creating zip archive", task.short_id())
                    downloaded = await zip_path(
                        downloaded,
                        task,
                        task.options.zip_password,
                        self.config.zip_compression_level,
                    )
                    self._raise_if_cancelled(task)

                self._record_result_manifest(task, downloaded)
                self._raise_if_cancelled(task)
                if task.destination in {
                    Destination.LOCAL_MOVIES,
                    Destination.LOCAL_SERIES,
                }:
                    task.phase = TaskPhase.DELIVERING
                    LOGGER.info("Task %s: phase=%s", task.short_id(), task.phase.value)
                    category = (
                        "movies"
                        if task.destination == Destination.LOCAL_MOVIES
                        else "series"
                    )
                    task.result_path = await deliver_to_local(
                        task, downloaded, self.config.local_download_root, category
                    )
                elif task.destination == Destination.TELEGRAM:
                    if telegram_client is None:
                        raise RuntimeError("Telegram client is unavailable")
                    task.phase = TaskPhase.UPLOADING
                    LOGGER.info("Task %s: phase=%s", task.short_id(), task.phase.value)
                    await self._run_or_cancel(
                        task,
                        upload_to_telegram(
                            task,
                            downloaded,
                            telegram_client,
                            self.config.telegram_leech_split_size,
                        ),
                    )
                else:
                    raise NotImplementedError(
                        f"{task.destination.value} delivery is not implemented"
                    )
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
        except (
            ArchiveCorruptError,
            ArchivePasswordError,
            ArchiveUnsupportedError,
        ) as exc:
            task.phase = TaskPhase.ERROR
            task.error = str(exc)
            LOGGER.warning("Task %s: %s", task.short_id(), task.error)
        except DuplicateTorrentError as exc:
            task.phase = TaskPhase.ERROR
            task.error = str(exc)
            LOGGER.warning("Task %s: %s", task.short_id(), task.error)
        except Exception as exc:
            if task.cancelled:
                task.phase = TaskPhase.CANCELLED
                LOGGER.info("Task %s: cancelled during shutdown", task.short_id())
            else:
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
        if task.source.type == SourceType.GOOGLE_DRIVE:
            return await download_gdrive(task)
        if task.source.type == SourceType.RCLONE:
            return await download_rclone(task, self.config.rclone_config_file)
        raise NotImplementedError(f"{task.source.type.value} download is planned but not implemented in this pass")

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None or task.phase in {
            TaskPhase.COMPLETE,
            TaskPhase.CANCELLED,
            TaskPhase.ERROR,
        }:
            return False
        task.cancelled = True
        task.cancel_event.set()
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
        return [task for task in self.tasks.values() if task.phase not in {TaskPhase.COMPLETE, TaskPhase.CANCELLED, TaskPhase.ERROR}]

    @staticmethod
    def _record_result_manifest(task: Task, path: Path) -> None:
        task.result_name = path.name
        if path.is_file():
            task.result_files = [path.name]
            task.result_folders = []
            return
        task.result_files = [
            item.relative_to(path).as_posix()
            for item in sorted(path.rglob("*"))
            if item.is_file()
        ]
        task.result_folders = [
            item.relative_to(path).as_posix()
            for item in sorted(path.rglob("*"))
            if item.is_dir()
        ]

    def _cleanup(self, path: Path) -> None:
        if path.exists():
            rmtree(path, ignore_errors=True)

    @staticmethod
    def _raise_if_cancelled(task: Task) -> None:
        if task.cancelled:
            raise asyncio.CancelledError()

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
