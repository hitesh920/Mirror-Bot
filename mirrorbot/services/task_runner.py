from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from time import time
from typing import TYPE_CHECKING

from ..core.errors import TaskFailure
from ..core.logging_config import log_event
from ..core.models import Destination, SourceType, Task, TaskPhase
from ..resolvers import resolve_source
from .archive import (
    ArchiveCorruptError,
    ArchivePasswordError,
    ArchiveUnsupportedError,
    extract_path,
    zip_path,
)
from .paths import ensure_no_symlinks
from .r2_delivery import upload_to_r2
from .telegram_delivery import upload_to_telegram
from .transfer_guard import TransferGuard, ensure_disk_space

if TYPE_CHECKING:
    from .task_manager import TaskManager

LOGGER = logging.getLogger(__name__)


class TaskRunner:
    """Owns transfer execution, terminal state, and per-task cleanup."""

    def __init__(self, manager: TaskManager):
        self.manager = manager

    async def run_task(
        self,
        task: Task,
        telegram_reply=None,
        telegram_client=None,
        on_selector_ready=None,
        on_selector_done=None,
    ) -> Task:
        manager = self.manager
        guard_job = None
        try:
            async with manager._queue_slot(manager.task_sem, task):
                ensure_disk_space(task.work_dir)
                guard_job = asyncio.create_task(TransferGuard(task).monitor())
                manager._raise_if_cancelled(task)
                task.transition(TaskPhase.DOWNLOADING)
                task.source = await manager._run_or_cancel(
                    task, resolve_source(task.source)
                )
                if (
                    task.source.metadata.get("batch_direct_only")
                    and task.source.type != SourceType.DIRECT_URL
                ):
                    raise TaskFailure("Batch link resolved to an unsupported source")
                manager._raise_if_cancelled(task)
                downloaded = await manager._run_or_cancel(
                    task,
                    manager._download(
                        task,
                        telegram_reply,
                        telegram_client,
                        on_selector_ready,
                        on_selector_done,
                    ),
                )

                manager._raise_if_cancelled(task)
                task.transition(TaskPhase.PREPARING)
                if not task.name:
                    task.name = downloaded.name
                task.current_file = downloaded.name
                downloaded = await self._process_download(task, downloaded)

                task.transition(TaskPhase.SCANNING)
                task.current_file = downloaded.name
                self._reset_progress(task)
                await asyncio.to_thread(ensure_no_symlinks, downloaded)
                await asyncio.to_thread(
                    manager._record_result_manifest, task, downloaded
                )
                manager._raise_if_cancelled(task)
                await self._deliver(task, downloaded, telegram_client)
                task.transition(TaskPhase.COMPLETE)
                task.current_file = ""
            self._log_completed(task)
        except asyncio.CancelledError:
            self._mark_cancelled(task)
        except TaskFailure as exc:
            self._mark_failed(task, exc, exc.category, logging.WARNING)
        except (
            ArchiveCorruptError,
            ArchivePasswordError,
            ArchiveUnsupportedError,
        ) as exc:
            self._mark_failed(task, exc, "processing", logging.WARNING)
        except Exception as exc:
            if task.cancelled:
                self._mark_cancelled(task, "cancelled during shutdown")
            else:
                self._mark_failed(task, exc, "unexpected", logging.ERROR)
                LOGGER.exception("Unexpected task failure task=%s", task.short_id())
        finally:
            await self._finalize(task, guard_job)
        return task

    async def _process_download(self, task: Task, downloaded: Path) -> Path:
        manager = self.manager
        if task.options.extract:
            manager._start_processing_phase(task, TaskPhase.EXTRACTING, downloaded)
            original = downloaded
            try:
                downloaded = await extract_path(
                    downloaded, task, task.options.extract_password
                )
            except (
                ArchiveCorruptError,
                ArchivePasswordError,
                ArchiveUnsupportedError,
            ):
                raise
            except RuntimeError as exc:
                task.processing_warnings.append(
                    "Extraction failed, so the original file was delivered."
                )
                LOGGER.warning(
                    "Task %s: extraction failed, falling back to original file: %s",
                    task.short_id(),
                    exc,
                )
                downloaded = original
                task.current_file = downloaded.name
                self._reset_progress(task)
            manager._raise_if_cancelled(task)
        if task.options.zip:
            manager._start_processing_phase(task, TaskPhase.ARCHIVING, downloaded)
            downloaded = await zip_path(
                downloaded,
                task,
                task.options.zip_password,
                0
                if task.source.type == SourceType.BATCH
                else manager.config.zip_compression_level,
                contents_only=task.source.type == SourceType.BATCH,
            )
            manager._raise_if_cancelled(task)
        return downloaded

    async def _deliver(self, task: Task, downloaded: Path, telegram_client) -> None:
        task.transition(TaskPhase.UPLOADING)
        task.current_file = downloaded.name
        await self._upload(task, downloaded, telegram_client)

    async def _upload(self, task: Task, path: Path, telegram_client) -> None:
        manager = self.manager
        if task.destination == Destination.TELEGRAM:
            if telegram_client is None:
                raise RuntimeError("Telegram client is unavailable")
            operation = upload_to_telegram(
                task,
                path,
                telegram_client,
                manager.config.telegram_leech_split_size,
                manager.config.telegram_dump_chat_id,
            )
        elif task.destination == Destination.CLOUDFLARE_R2:
            operation = upload_to_r2(task, path, manager.config)
        else:
            raise NotImplementedError(
                f"{task.destination.value} upload is not implemented"
            )
        await manager._run_or_cancel(task, operation)

    async def _finalize(self, task: Task, guard_job: asyncio.Task | None) -> None:
        manager = self.manager
        if guard_job:
            guard_job.cancel()
            await asyncio.gather(guard_job, return_exceptions=True)
        if task.torrent_hash and task.phase in {
            TaskPhase.CANCELLED,
            TaskPhase.ERROR,
        }:
            try:
                await manager.qb.delete(task.torrent_hash, True)
            except Exception:
                LOGGER.exception(
                    "Task %s: failed to clean qBittorrent task", task.short_id()
                )
        manager._cleanup(task.work_dir)
        log_event(
            LOGGER,
            logging.INFO,
            "task.cleaned",
            task=task.short_id(),
            phase=task.phase.value,
        )
        manager._prune_terminal_tasks()

    @staticmethod
    def _reset_progress(task: Task) -> None:
        task.begin_progress()

    @staticmethod
    def _log_completed(task: Task) -> None:
        log_event(
            LOGGER,
            logging.INFO,
            "task.completed",
            task=task.short_id(),
            phase=task.phase.value,
            engine=task.source.type.value,
            destination=task.destination.value,
            duration=f"{int(time() - task.created_at)}s",
        )

    @staticmethod
    def _mark_cancelled(task: Task, fallback: str = "cancelled") -> None:
        task.transition(TaskPhase.CANCELLED)
        task.cancelled = True
        log_event(
            LOGGER,
            logging.INFO,
            "task.cancelled",
            task=task.short_id(),
            phase=task.phase.value,
            result=task.cancel_reason or fallback,
        )

    @staticmethod
    def _mark_failed(
        task: Task,
        error: Exception,
        category: str,
        level: int,
    ) -> None:
        task.transition(TaskPhase.ERROR)
        task.error = str(error)
        task.failure_category = category
        log_event(
            LOGGER,
            level,
            "task.failed",
            task=task.short_id(),
            phase=task.phase.value,
            error_category=category,
            result=error,
        )
