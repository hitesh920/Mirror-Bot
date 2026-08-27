"""Phase 5c: archive wait-not-poll (N11) and the global HTTP download cap (N10)."""

import asyncio

import pytest
from aiohttp import web

from mirrorbot.core.models import TaskPhase
from mirrorbot.downloaders import direct as direct_module
from mirrorbot.services import archive


async def test_run_terminates_promptly_on_cancel(make_task, monkeypatch):
    """_run must react to task.cancel_event, not busy-poll returncode."""
    terminated = {}

    async def fake_terminate(process):
        terminated["pid"] = process.pid
        process.terminate()
        await process.wait()

    monkeypatch.setattr(archive, "terminate_process", fake_terminate)
    task = make_task(phase=TaskPhase.EXTRACTING, size=1)

    runner = asyncio.create_task(archive._run(task, "sleep", "30"))
    await asyncio.sleep(0.1)
    task.request_cancel("stop")

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(runner, timeout=3)
    assert "pid" in terminated


def test_download_slots_is_a_singleton_semaphore():
    direct_module._http_download_slots = None
    first = direct_module._download_slots()
    second = direct_module._download_slots()
    assert first is second
    assert isinstance(first, asyncio.Semaphore)


async def test_direct_downloads_respect_the_global_slot_cap(make_task, monkeypatch):
    monkeypatch.setattr(direct_module, "HTTP_DOWNLOAD_CONCURRENCY", 2)
    direct_module._http_download_slots = None

    active = 0
    peak = 0

    app = web.Application()

    async def slow(_request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            resp = web.StreamResponse()
            await resp.prepare(_request)
            for _ in range(3):
                await resp.write(b"chunk")
                await asyncio.sleep(0.05)
            await resp.write_eof()
            return resp
        finally:
            active -= 1

    app.router.add_get("/{name}", slow)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    async def one(name):
        task = make_task(value=f"http://127.0.0.1:{port}/{name}")
        await direct_module.download_single_direct(task)

    try:
        await asyncio.gather(*(one(f"f{i}") for i in range(5)))
    finally:
        await runner.cleanup()
        direct_module._http_download_slots = None

    assert peak <= 2
