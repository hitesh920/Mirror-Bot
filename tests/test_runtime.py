import asyncio

import pytest

from mirrorbot.services.background import BackgroundTasks
from mirrorbot.services.runtime import RuntimeCoordinator


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