"""Shared fixtures and fakes for the orchestration-layer tests."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from mirrorbot.core.models import (
    AddOptions,
    Destination,
    Source,
    SourceType,
    Task,
)


@pytest.fixture
def make_task(tmp_path: Path):
    """Return a factory that builds a Task rooted under the test's tmp_path."""

    def _make(
        *,
        source_type: SourceType = SourceType.DIRECT_URL,
        value: str = "https://example.com/file.bin",
        destination: Destination = Destination.CLOUDFLARE_R2,
        options: AddOptions | None = None,
        **overrides,
    ) -> Task:
        task_id = str(uuid4())
        task = Task(
            id=task_id,
            user_id=1,
            chat_id=1,
            message_id=77,
            source=Source(source_type, value),
            destination=destination,
            options=options or AddOptions(),
            work_dir=tmp_path / task_id,
        )
        for key, attr in overrides.items():
            setattr(task, key, attr)
        return task

    return _make


class FakeClock:
    """Deterministic monotonic() replacement."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
