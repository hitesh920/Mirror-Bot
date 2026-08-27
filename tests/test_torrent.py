"""Coverage for the torrent download lifecycle (#27)."""

import asyncio
from pathlib import Path

import pytest

from mirrorbot.core.errors import (
    TorrentEngineError,
    TorrentMetadataTimeoutError,
    TorrentRemovedError,
)
from mirrorbot.core.models import TaskPhase
from mirrorbot.downloaders import torrent as torrent_module
from mirrorbot.downloaders.torrent import (
    DuplicateTorrentError,
    _wait_for_metadata,
    _wait_for_torrent,
    download_torrent,
    magnet_info_hash,
)

HASH = "0123456789abcdef0123456789abcdef01234567"
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=Example"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    real_sleep = asyncio.sleep

    async def _sleep(_seconds):
        # Yield to the loop (so pending tasks progress) without wall-clock delay.
        await real_sleep(0)

    monkeypatch.setattr(torrent_module.asyncio, "sleep", _sleep)
    monkeypatch.setattr(torrent_module, "ensure_disk_space", lambda *a, **k: None)


class FakeQB:
    """Minimal scripted qBittorrent client."""

    def __init__(self, *, torrents_by_tag=None, torrents_by_hash=None, files=None):
        self._by_tag = torrents_by_tag if torrents_by_tag is not None else []
        self._by_hash = torrents_by_hash if torrents_by_hash is not None else []
        self._files = files or []
        self.deleted = []
        self.started = []
        self.stopped = []
        self.priorities = []
        self.added = []

    async def info(self, *, tag="", torrent_hash=""):
        source = self._by_tag if tag else self._by_hash
        return list(source() if callable(source) else source)

    async def files(self, torrent_hash):
        return list(self._files() if callable(self._files) else self._files)

    async def add(self, source, save_path, tag):
        self.added.append((source, save_path, tag))

    async def stop(self, torrent_hash):
        self.stopped.append(torrent_hash)

    async def start(self, torrent_hash):
        self.started.append(torrent_hash)

    async def delete(self, torrent_hash, delete_files):
        self.deleted.append((torrent_hash, delete_files))

    async def set_file_priority(self, torrent_hash, file_ids, priority):
        self.priorities.append((torrent_hash, file_ids, priority))


def _selection(torrent_hash):
    from mirrorbot.downloaders.torrent_selector import Selection

    return Selection(
        token="tok",
        torrent_hash=torrent_hash,
        files=[],
        submitted=asyncio.Event(),
        closed=asyncio.Event(),
    )


class ImmediateSelector:
    """Selector that resolves the selection as soon as it is asked."""

    def __init__(self):
        self.public_base_url = "http://host:8001"
        self.cancelled = []

    def select(self, torrent_hash, files):
        async def _run():
            return "done"

        return _run()

    def get(self, torrent_hash):
        return _selection(torrent_hash)

    async def cancel(self, torrent_hash):
        self.cancelled.append(torrent_hash)


class CancelOnGetSelector:
    """Selector that flips the task to cancelled the first time it is polled."""

    def __init__(self, task):
        self.public_base_url = "http://host:8001"
        self.task = task
        self.cancelled = []

    def select(self, torrent_hash, files):
        async def _run():
            await asyncio.Event().wait()

        return _run()

    def get(self, torrent_hash):
        self.task.cancelled = True
        return

    async def cancel(self, torrent_hash):
        self.cancelled.append(torrent_hash)


def test_magnet_info_hash_parses_hex_and_base32():
    import base64

    b32 = base64.b32encode(bytes.fromhex(HASH)).decode()
    assert magnet_info_hash(MAGNET) == HASH
    assert magnet_info_hash(f"magnet:?xt=urn:btih:{b32}") == HASH
    assert magnet_info_hash("magnet:?dn=no-hash") == ""


async def test_duplicate_magnet_is_rejected_before_add(make_task):
    task = make_task()
    task.source.value = MAGNET
    qb = FakeQB(torrents_by_hash=[{"hash": HASH}])

    with pytest.raises(DuplicateTorrentError):
        await download_torrent(task, qb, ImmediateSelector())

    assert qb.added == []


async def test_wait_for_torrent_times_out_when_never_added(make_task):
    task = make_task()
    qb = FakeQB(torrents_by_tag=[])

    with pytest.raises(TorrentEngineError, match="did not add"):
        await _wait_for_torrent(qb, task)


async def test_wait_for_metadata_reports_removed_torrent(make_task):
    task = make_task()
    task.torrent_hash = HASH
    qb = FakeQB(torrents_by_hash=[])

    with pytest.raises(TorrentRemovedError):
        await _wait_for_metadata(qb, task)


async def test_wait_for_metadata_times_out(make_task, monkeypatch):
    monkeypatch.setattr(torrent_module, "TORRENT_METADATA_TIMEOUT", 3)
    task = make_task()
    task.torrent_hash = HASH
    qb = FakeQB(
        torrents_by_hash=[{"hash": HASH, "state": "metaDL", "progress": 0}],
        files=[],
    )

    with pytest.raises(TorrentMetadataTimeoutError):
        await _wait_for_metadata(qb, task)


async def test_full_lifecycle_returns_content_path_and_cleans_up(make_task, tmp_path):
    task = make_task()
    torrent_file = tmp_path / "example.torrent"
    torrent_file.write_bytes(b"d8:announce")
    save_path = task.work_dir
    save_path.mkdir(parents=True, exist_ok=True)
    content = save_path / "Example.mkv"
    content.write_bytes(b"data")

    finished = {
        "hash": HASH,
        "name": "Example",
        "state": "uploading",
        "progress": 1.0,
        "downloaded": 4,
        "size": 4,
        "dlspeed": 0,
        "eta": 0,
        "content_path": str(content),
        "save_path": str(save_path),
    }
    files = [{"index": 0, "name": "Example.mkv", "size": 4, "priority": 1}]
    qb = FakeQB(
        torrents_by_tag=[finished],
        torrents_by_hash=[finished],
        files=files,
    )

    ready_calls, done_calls = [], []

    async def on_ready(t):
        ready_calls.append(t.selection_url)
        return "selector-msg"

    async def on_done(msg):
        done_calls.append(msg)

    result = await download_torrent(
        task,
        qb,
        ImmediateSelector(),
        torrent_file=torrent_file,
        on_selector_ready=on_ready,
        on_selector_done=on_done,
    )

    assert result == Path(str(content))
    assert (HASH, False) in qb.deleted
    assert ready_calls and done_calls == ["selector-msg"]
    assert task.phase == TaskPhase.DOWNLOADING


async def test_cancel_during_selection_deletes_torrent(make_task, tmp_path):
    task = make_task()
    torrent_file = tmp_path / "example.torrent"
    torrent_file.write_bytes(b"d8:announce")

    ready = {
        "hash": HASH,
        "name": "Example",
        "state": "pausedDL",
        "progress": 0.0,
        "downloaded": 0,
        "size": 4,
    }
    files = [{"index": 0, "name": "Example.mkv", "size": 4, "priority": 1}]
    qb = FakeQB(torrents_by_tag=[ready], torrents_by_hash=[ready], files=files)

    with pytest.raises(asyncio.CancelledError):
        await download_torrent(
            task, qb, CancelOnGetSelector(task), torrent_file=torrent_file
        )

    assert (HASH, True) in qb.deleted
