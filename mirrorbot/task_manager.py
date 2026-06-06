import asyncio
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

from .archive import extract_path, zip_path
from .config import Config
from .downloaders.direct import download_direct
from .downloaders.telegram import download_telegram_file
from .downloaders.ytdlp import download_ytdlp
from .models import Destination, SourceType, Task, TaskPhase
from .paths import deliver_to_local
from .resolvers import resolve_source


class TaskManager:
    def __init__(self, config: Config):
        self.config = config
        self.tasks: dict[str, Task] = {}
        self.download_sem = asyncio.Semaphore(config.queue_download_limit)
        self.upload_sem = asyncio.Semaphore(config.queue_upload_limit)

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
        return task

    async def run_local_task(self, task: Task, telegram_reply=None) -> Task:
        try:
            async with self.download_sem:
                task.phase = TaskPhase.DOWNLOADING
                task.source = await resolve_source(task.source)
                downloaded = await self._download(task, telegram_reply)

            task.phase = TaskPhase.PROCESSING
            if task.options.extract:
                downloaded = await extract_path(downloaded, task.options.extract_password)
            if task.options.zip:
                downloaded = await zip_path(downloaded, task.options.zip_password)

            async with self.upload_sem:
                task.phase = TaskPhase.DELIVERING
                category = "movies" if task.destination == Destination.LOCAL_MOVIES else "series"
                task.result_path = deliver_to_local(downloaded, self.config.local_download_root, category)
            task.phase = TaskPhase.COMPLETE
        except asyncio.CancelledError:
            task.phase = TaskPhase.CANCELLED
            task.cancelled = True
        except Exception as exc:
            task.phase = TaskPhase.ERROR
            task.error = str(exc)
        finally:
            self._cleanup(task.work_dir)
        return task

    async def _download(self, task: Task, telegram_reply=None) -> Path:
        if task.source.type == SourceType.TELEGRAM_FILE:
            return await download_telegram_file(task, telegram_reply)
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

