from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..core.models import Task

if TYPE_CHECKING:
    from .task_manager import TaskManager


class TaskRunner:
    """Runs task pipelines while TaskManager owns task registry and cancellation."""

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
        return await self.manager._run_task_pipeline(
            task,
            telegram_reply=telegram_reply,
            telegram_client=telegram_client,
            on_selector_ready=on_selector_ready,
            on_selector_done=on_selector_done,
        )

    async def run_local_upload(self, task: Task, path: Path, telegram_client) -> Task:
        return await self.manager._run_local_upload_pipeline(task, path, telegram_client)
