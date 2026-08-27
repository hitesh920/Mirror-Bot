"""Phase 1 targeted correctness fixes: N2, N3, N4, N5."""

import asyncio

import pytest
from aiohttp import web

from mirrorbot import resolvers as resolvers_pkg
from mirrorbot.core.errors import NetworkError, TorrentEngineError
from mirrorbot.core.models import Source, SourceType
from mirrorbot.downloaders import direct as direct_module
from mirrorbot.downloaders import torrent as torrent_module
from mirrorbot.downloaders.direct import download_collection
from mirrorbot.downloaders.torrent import _clean_skipped_files, download_torrent
from mirrorbot.resolvers import base as resolver_base
from mirrorbot.resolvers import resolve_source

_REAL_SLEEP = asyncio.sleep


async def _fast_sleep(_seconds):
    """Yield to the loop without wall-clock delay (safe under global patching)."""
    await _REAL_SLEEP(0)


# --- N5: skipped-file cleanup tolerates string priorities -------------------


def test_clean_skipped_files_keeps_wanted_with_string_priority(make_task, tmp_path):
    task = make_task()
    save_path = task.work_dir
    save_path.mkdir(parents=True, exist_ok=True)
    wanted = save_path / "keep.mkv"
    skipped = save_path / "drop.mkv"
    wanted.write_bytes(b"keep")
    skipped.write_bytes(b"drop")

    torrent = {"save_path": str(save_path), "content_path": str(save_path)}
    files = [
        {"name": "keep.mkv", "priority": "1"},
        {"name": "drop.mkv", "priority": "0"},
    ]

    _clean_skipped_files(task, torrent, files)

    assert wanted.exists()
    assert not skipped.exists()


# --- N2: selector that never registers a selection fails instead of spinning -


class NeverRegistersSelector:
    def __init__(self):
        self.public_base_url = "http://host:8001"

    def select(self, torrent_hash, files):
        async def _run():
            return None

        return _run()

    async def get(self, torrent_hash):
        return None

    async def cancel(self, torrent_hash):
        pass


async def test_selection_that_never_registers_raises_engine_error(
    make_task, tmp_path, monkeypatch
):
    monkeypatch.setattr(torrent_module.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(torrent_module, "ensure_disk_space", lambda *a, **k: None)

    task = make_task()
    torrent_file = tmp_path / "x.torrent"
    torrent_file.write_bytes(b"d8:announce")
    hash_ = "0" * 40
    ready = {
        "hash": hash_,
        "name": "X",
        "state": "pausedDL",
        "progress": 0.0,
        "downloaded": 0,
        "size": 1,
    }

    class QB:
        def __init__(self):
            self.deleted = []

        async def info(self, *, tag="", torrent_hash=""):
            return [ready]

        async def files(self, torrent_hash):
            return [{"index": 0, "name": "X", "size": 1, "priority": 1}]

        async def add(self, *a):
            pass

        async def stop(self, *a):
            pass

        async def delete(self, h, delete_files):
            self.deleted.append((h, delete_files))

    qb = QB()
    with pytest.raises(TorrentEngineError, match="before it started"):
        await download_torrent(
            task, qb, NeverRegistersSelector(), torrent_file=torrent_file
        )
    assert (hash_, True) in qb.deleted


# --- N3: a redirect chain that never resolves raises ResolverError ----------


class LoopResolver:
    name = "loop"

    def supports(self, url: str) -> bool:
        return "loop.test" in url

    async def resolve(self, url, session):
        depth = int(url.rsplit("/", 1)[-1] or "0")
        return resolver_base.ResolvedDownload(f"https://loop.test/{depth + 1}")


async def test_endless_redirect_chain_raises_resolver_error(monkeypatch):
    monkeypatch.setattr(resolvers_pkg, "RESOLVERS", (LoopResolver(),))

    with pytest.raises(resolver_base.ResolverError, match="kept redirecting"):
        await resolve_source(Source(SourceType.DIRECT_URL, "https://loop.test/0"))


# --- N4: collection downloads tolerate partial failure & retry --------------


@pytest.fixture
async def flaky_server():
    app = web.Application()
    state = {"hits": 0}

    async def ok(_request):
        return web.Response(body=b"good-bytes")

    async def flaky(_request):
        state["hits"] += 1
        if state["hits"] == 1:
            return web.Response(status=503)
        return web.Response(body=b"recovered")

    async def broken(_request):
        return web.Response(status=500)

    app.router.add_get("/ok", ok)
    app.router.add_get("/flaky", flaky)
    app.router.add_get("/broken", broken)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        await runner.cleanup()


def _collection(base: str, names_paths):
    files = [
        resolver_base.ResolvedFile(url=f"{base}/{path}", filename=name)
        for name, path in names_paths
    ]
    return resolver_base.ResolvedCollection(title="bundle", files=files)


async def test_collection_retries_transient_failure(
    make_task, flaky_server, monkeypatch
):
    monkeypatch.setattr(direct_module.asyncio, "sleep", _fast_sleep)
    base, _state = flaky_server
    task = make_task()
    task.source = Source(SourceType.DIRECT_URL, base, metadata={})
    collection = _collection(base, [("a.bin", "ok"), ("b.bin", "flaky")])

    root = await download_collection(task, collection)

    assert (root / "a.bin").read_bytes() == b"good-bytes"
    assert (root / "b.bin").read_bytes() == b"recovered"
    assert not task.processing_warnings


async def test_collection_tolerates_partial_failure(
    make_task, flaky_server, monkeypatch
):
    monkeypatch.setattr(direct_module.asyncio, "sleep", _fast_sleep)
    base, _state = flaky_server
    task = make_task()
    task.source = Source(SourceType.DIRECT_URL, base, metadata={})
    collection = _collection(base, [("a.bin", "ok"), ("b.bin", "broken")])

    root = await download_collection(task, collection)

    assert (root / "a.bin").read_bytes() == b"good-bytes"
    assert not (root / "b.bin").exists()
    assert any("b.bin" in w for w in task.processing_warnings)


async def test_collection_all_failing_raises(make_task, flaky_server, monkeypatch):
    monkeypatch.setattr(direct_module.asyncio, "sleep", _fast_sleep)
    base, _state = flaky_server
    task = make_task()
    task.source = Source(SourceType.DIRECT_URL, base, metadata={})
    collection = _collection(base, [("a.bin", "broken"), ("b.bin", "broken")])

    with pytest.raises(NetworkError):
        await download_collection(task, collection)
