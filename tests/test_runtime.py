import asyncio
import json
import os
from time import time

import pytest

from mirrorbot.services.background import BackgroundTasks
from mirrorbot.services.runtime import RuntimeCoordinator
from mirrorbot.services.restart_state import save_restart_state, take_restart_state


class FakeManager:
    def __init__(self):
        self.timeouts = []

    async def shutdown(self, timeout):
        self.timeouts.append(timeout)


@pytest.mark.asyncio
async def test_runtime_shutdown_is_idempotent_and_cleans_services():
    manager = FakeManager()
    background = BackgroundTasks()
    background.create(asyncio.sleep(60), name="pending")
    cleaned = []

    async def cleanup():
        cleaned.append(True)

    runtime = RuntimeCoordinator(manager, background, timeout=3)
    await runtime.shutdown((cleanup,))
    await runtime.shutdown((cleanup,))

    assert manager.timeouts == [3]
    assert cleaned == [True]
    assert not background.tasks


def test_restart_state_round_trip_and_permissions(tmp_path):
    path = tmp_path / ".restart.json"
    saved = save_restart_state(123, 456, path)
    assert os.stat(path).st_mode & 0o777 == 0o600
    loaded = take_restart_state(path)

    assert loaded == saved
    assert not path.exists()


def test_restart_state_rejects_stale_and_invalid_markers(tmp_path):
    path = tmp_path / ".restart.json"
    path.write_text(
        json.dumps({"chat_id": 1, "message_id": 2, "requested_at": time() - 1000}),
        encoding="utf-8",
    )
    assert take_restart_state(path) is None
    assert not path.exists()

    path.write_text("invalid", encoding="utf-8")
    assert take_restart_state(path) is None
    assert not path.exists()
