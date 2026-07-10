import asyncio
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.client_exceptions import ClientResponseError

from mirrorbot.core.errors import (
    NetworkTimeoutError,
    TorrentDuplicateError,
    TorrentEngineError,
    TorrentMetadataTimeoutError,
    TorrentRemovedError,
)
from mirrorbot.core.models import AddOptions, Destination, Source, SourceType, Task, TaskPhase
from mirrorbot.core.parser import parse_add_text
from mirrorbot.core.source_detector import detect_source
from mirrorbot.downloaders.direct import retryable_direct_error
from mirrorbot.downloaders.torrent import selected_torrent_size
from mirrorbot.downloaders.torrent_selector import TorrentSelector
from mirrorbot.services import media_metadata
from mirrorbot.services import google_drive_delivery as gdrive_delivery
from mirrorbot.services import telegram_delivery
from mirrorbot.services.buzzheavier_delivery import (
    buzzheavier_upload_name,
    duplicate_basenames,
)
from mirrorbot.services.file_explorer import PAGE as FILE_EXPLORER_PAGE
from mirrorbot.services.file_explorer import FileExplorer
from mirrorbot.services.page_style import TEMP_PAGE_CSS
from mirrorbot.services.google_drive_delivery import GoogleDriveUploader, next_drive_chunk
from mirrorbot.services.local_delivery import deliver_to_local
from mirrorbot.services.media_library import MediaMatch
from mirrorbot.services.task_manager import MAX_TERMINAL_TASKS, TaskManager
from mirrorbot.services.telegram_delivery import (
    telegram_chat_id,
    telegram_message_link,
    upload_files,
    upload_to_telegram,
)
from mirrorbot.services.web.auth import credentials_match, is_public_path
from mirrorbot.services.web_dashboard import WebDashboard
from mirrorbot.telegram.messages import completion_message, completion_payload
from mirrorbot.telegram.state import ExpiringStore


def make_task() -> Task:
    return Task(
        id=str(uuid4()),
        user_id=1,
        chat_id=1,
        message_id=1,
        source=Source(SourceType.DIRECT_URL, "https://example.com/file.bin"),
        destination=Destination.TELEGRAM,
        options=AddOptions(),
        work_dir=Path("unused"),
    )


def test_parse_add_text_flags():
    link, options = parse_add_text("/add https://example.com/a.zip -zp secret -e -n custom")
    assert link == "https://example.com/a.zip"
    assert options.zip is True
    assert options.zip_password == "secret"
    assert options.extract is True
    assert options.name == "custom"


def test_detect_source_common_inputs():
    assert detect_source("magnet:?xt=urn:btih:abcd").type == SourceType.MAGNET
    assert detect_source("https://drive.google.com/file/d/abc/view").type == SourceType.GOOGLE_DRIVE
    assert detect_source("https://example.com/file.bin").type == SourceType.DIRECT_URL


def test_task_cancel_is_idempotent():
    task = make_task()
    assert task.request_cancel("test") is True
    assert task.request_cancel("again") is False
    assert task.cancelled is True
    assert task.cancel_event.is_set()
    assert task.cancel_reason == "test"


def test_telegram_dump_chat_id_parser():
    assert telegram_chat_id("") is None
    assert telegram_chat_id("-1001234567890") == -1001234567890
    assert telegram_chat_id("@dump_channel") == "@dump_channel"


def test_private_channel_message_link_fallback():
    message = SimpleNamespace(id=77, link=None)

    assert (
        telegram_message_link(message, -1001234567890, 123)
        == "https://t.me/c/1234567890/77"
    )


def test_telegram_completion_mentions_dump_channel():
    task = make_task()
    task.result_name = "video.mkv"
    task.result_files = ["video.mkv"]
    task.result_links = ["https://t.me/c/123/77"]
    task.telegram_upload_mode = "dump_channel"

    text = completion_message(task)

    assert "Telegram dump channel" in text


def test_telegram_completion_payload_includes_result_links():
    task = make_task()
    task.result_links = ["https://t.me/c/123/77"]

    payload = completion_payload(task, "http://jellyfin.local")

    assert payload["links"] == [{"label": "Open 1", "url": "https://t.me/c/123/77"}]


def test_buzzheavier_duplicate_upload_names_preserve_relative_context():
    files = [
        (Path("season1/video.mkv"), "season1/video.mkv"),
        (Path("season2/video.mkv"), "season2/video.mkv"),
        (Path("poster.jpg"), "poster.jpg"),
    ]
    duplicates = duplicate_basenames(files)

    assert duplicates == {"video.mkv"}
    assert buzzheavier_upload_name("season1/video.mkv", duplicates) == "season1 - video.mkv"
    assert buzzheavier_upload_name("poster.jpg", duplicates) == "poster.jpg"


def test_upload_tree_rejects_symbolic_links(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    try:
        os.symlink(secret, source / "innocent.txt")
    except OSError:
        pytest.skip("Symbolic links are unavailable in this environment")

    with pytest.raises(RuntimeError, match="Symbolic links are not allowed"):
        upload_files(source)


def test_mobile_action_bars_reserve_list_space():
    torrent_page_source = Path(
        "mirrorbot/downloaders/torrent_selector.py"
    ).read_text(encoding="utf-8")

    assert "--selectionbar-height" in torrent_page_source
    assert "ResizeObserver(syncSelectionSpace)" in torrent_page_source
    assert "--selectionbar-height" in FILE_EXPLORER_PAGE
    assert "ResizeObserver(syncSelectionSpace)" in FILE_EXPLORER_PAGE
    assert "[hidden] { display: none !important; }" in TEMP_PAGE_CSS


def test_container_shutdown_keeps_qbittorrent_alive_for_bot_cleanup():
    script = Path("start.sh").read_text(encoding="utf-8")
    shutdown_block = script.split("shutdown() {", 1)[1].split("}", 1)[0]

    assert 'kill -TERM "$bot_pid"' in shutdown_block
    assert '"$qbit_pid"' not in shutdown_block
    assert script.index('wait "$bot_pid"') < script.index('kill -TERM "$qbit_pid"')


def test_selected_torrent_size_uses_current_priorities():
    files = [
        {"size": 100, "priority": 0},
        {"size": 200, "priority": 1},
        {"size": 300, "priority": 6},
    ]

    assert selected_torrent_size(files) == 500


def test_terminal_transition_is_not_overwritten():
    task = make_task()
    task.transition(TaskPhase.COMPLETE)
    task.transition(TaskPhase.ERROR)
    assert task.phase == TaskPhase.COMPLETE


def test_expiring_store_take_and_expiry():
    store = ExpiringStore[str](ttl_seconds=30)
    store.put("token", "value")
    assert store.get("token") == "value"
    assert store.take("token") == "value"
    assert store.take("token") is None

    expired = ExpiringStore[str](ttl_seconds=-1)
    expired.put("old", "value")
    assert expired.get("old") is None


def test_web_auth_helpers():
    assert is_public_path("/login")
    assert is_public_path("/assets/index.js")
    assert not is_public_path("/api/state")
    assert credentials_match("owner", "secret", "owner", "secret")
    assert not credentials_match("owner", "secret", "owner", "wrong")


def test_web_destination_validation_accepts_aliases():
    dashboard = WebDashboard.__new__(WebDashboard)
    assert dashboard.destination_from_form("local", "series") == Destination.LOCAL_SERIES
    assert dashboard.destination_from_form("gdrive") == Destination.GOOGLE_DRIVE
    assert dashboard.destination_from_form("google_drive") == Destination.GOOGLE_DRIVE

    with pytest.raises(Exception):
        dashboard.destination_from_form("")


def test_torrent_failures_have_specific_categories():
    assert TorrentMetadataTimeoutError.category == "torrent_metadata_timeout"
    assert TorrentRemovedError.category == "torrent_removed"
    assert TorrentDuplicateError.category == "torrent_duplicate"
    assert TorrentEngineError.category == "torrent_engine"


def test_network_timeout_has_specific_category():
    assert NetworkTimeoutError.category == "network_timeout"


def test_direct_retry_classifier():
    assert retryable_direct_error(NetworkTimeoutError("timeout"))
    assert retryable_direct_error(
        ClientResponseError(None, (), status=503, message="temporary")
    )
    assert not retryable_direct_error(
        ClientResponseError(None, (), status=404, message="missing")
    )


@pytest.mark.asyncio
async def test_probe_media_reads_video_metadata(monkeypatch):
    async def fake_run(*_args, timeout=60):
        return 0, (
            '{"format":{"duration":"42.4","tags":{"artist":"A","title":"T"}},'
            '"streams":['
            '{"codec_type":"video","codec_name":"h264","width":1920,"height":1080},'
            '{"codec_type":"audio","codec_name":"aac"}'
            ']}'
        ), ""

    monkeypatch.setattr(media_metadata, "_run_command", fake_run)

    metadata = await media_metadata.probe_media(Path("video.mkv"))

    assert metadata.is_video is True
    assert metadata.is_audio is True
    assert metadata.duration == 42
    assert metadata.width == 1920
    assert metadata.height == 1080
    assert metadata.artist == "A"
    assert metadata.title == "T"


@pytest.mark.asyncio
async def test_series_delivery_skips_empty_original_release_folder(tmp_path):
    local_root = tmp_path / "media"
    downloaded = tmp_path / "extracted" / "You.S01.720p.Hindi.Eng.Vegamovies.NL"
    downloaded.mkdir(parents=True)
    episode = downloaded / "You.S01E01.720p.Hindi.English.Vegamovies.NL.mkv"
    episode.write_bytes(b"episode")
    task = make_task()
    task.work_dir = tmp_path / "work"
    match = MediaMatch("tv", "You", "2018", season=1, confidence=1.0)

    await deliver_to_local(task, downloaded.parent, local_root, "series", match)

    target = local_root / "series" / "You (2018)"
    assert (
        target / "Season 01" / "You.S01E01.720p.Hindi.English.Vegamovies.NL.mkv"
    ).is_file()
    assert not (target / "You.S01.720p.Hindi.Eng.Vegamovies.NL").exists()


@pytest.mark.asyncio
async def test_movie_delivery_flattens_single_release_wrapper_folder(tmp_path):
    local_root = tmp_path / "media"
    downloaded = tmp_path / "extracted" / "Obsession.2026.1080p.WEB-DL"
    downloaded.mkdir(parents=True)
    movie = downloaded / "Obsession.2026.1080p.WEB-DL.mkv"
    movie.write_bytes(b"movie")
    task = make_task()
    task.destination = Destination.LOCAL_MOVIES
    task.work_dir = tmp_path / "work"
    match = MediaMatch("movie", "Obsession", "2026", confidence=1.0)

    await deliver_to_local(task, downloaded.parent, local_root, "movies", match)

    target = local_root / "movies" / "Obsession (2026)"
    assert (target / "Obsession.2026.1080p.WEB-DL.mkv").is_file()
    assert not (target / "Obsession.2026.1080p.WEB-DL").exists()


@pytest.mark.asyncio
async def test_gdrive_upload_cleans_partial_files_on_failure(tmp_path, monkeypatch):
    source = tmp_path / "file.bin"
    source.write_bytes(b"data")
    task = make_task()
    task.destination = Destination.GOOGLE_DRIVE

    uploader = GoogleDriveUploader.__new__(GoogleDriveUploader)
    uploader.task = task
    uploader.path = source
    uploader.config = SimpleNamespace(google_drive_folder_id="root")
    uploader.service = object()
    uploader.created_ids = ["created-file"]
    uploader.total_size = source.stat().st_size
    uploader.uploaded_base = 0
    uploader.started = 1
    uploader.upload_file = AsyncMock(side_effect=RuntimeError("network failed"))
    uploader.cleanup_created = AsyncMock()
    monkeypatch.setattr(
        gdrive_delivery,
        "unique_drive_name",
        lambda _service, _parent_id, name: name,
    )

    with pytest.raises(RuntimeError, match="network failed"):
        await uploader.upload()

    uploader.cleanup_created.assert_awaited_once_with("failed")


@pytest.mark.asyncio
async def test_gdrive_cancel_captures_file_created_by_inflight_chunk():
    started = Event()
    release = Event()

    class Request:
        def next_chunk(self):
            started.set()
            release.wait(timeout=5)
            return None, {"id": "created-after-cancel"}

    created_ids = []
    operation = asyncio.create_task(next_drive_chunk(Request(), created_ids))
    await asyncio.to_thread(started.wait, 2)
    operation.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await operation

    assert created_ids == ["created-after-cancel"]


@pytest.mark.asyncio
async def test_telegram_timeout_does_not_fallback_and_duplicate(monkeypatch, tmp_path):
    source = tmp_path / "file.bin"
    source.write_bytes(b"data")
    task = make_task()
    upload = AsyncMock(side_effect=TimeoutError("ambiguous timeout"))
    monkeypatch.setattr(telegram_delivery, "_upload_to_telegram_chat", upload)

    with pytest.raises(TimeoutError, match="ambiguous timeout"):
        await upload_to_telegram(task, source, object(), 100, "-1001234567890")

    upload.assert_awaited_once()


@pytest.mark.asyncio
async def test_file_explorer_delete_runs_in_worker_thread(tmp_path, monkeypatch):
    target = tmp_path / "delete-me.bin"
    target.write_bytes(b"data")
    explorer = FileExplorer.__new__(FileExplorer)
    explorer.root = tmp_path
    explorer._session = lambda _request: SimpleNamespace(token="token")
    explorer._schedule_scan = lambda *_args: None
    worker_calls = []
    original_to_thread = asyncio.to_thread

    async def tracked_to_thread(function, *args, **kwargs):
        worker_calls.append(function)
        return await original_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", tracked_to_thread)

    class Request:
        match_info = {"action": "delete"}

        async def json(self):
            return {"sources": ["delete-me.bin"]}

    response = await explorer._action(Request())

    assert response.status == 200
    assert explorer._delete_paths in worker_calls
    assert not target.exists()


def test_gdrive_permission_creation_retries(monkeypatch):
    class PermissionCreate:
        def __init__(self):
            self.calls = 0

        def execute(self):
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary")

    class Permissions:
        def __init__(self, create):
            self.create_call = create

        def create(self, **_kwargs):
            return self.create_call

    class Service:
        def __init__(self, create):
            self.create_call = create

        def permissions(self):
            return Permissions(self.create_call)

    create_call = PermissionCreate()
    task = make_task()
    uploader = GoogleDriveUploader.__new__(GoogleDriveUploader)
    uploader.task = task
    uploader.service = Service(create_call)
    monkeypatch.setattr(gdrive_delivery, "sleep", lambda _seconds: None)

    uploader.set_public_permission("file-id")

    assert create_call.calls == 2


@pytest.mark.asyncio
async def test_torrent_selector_tracks_multiple_pending_selections():
    selector = TorrentSelector(object(), "http://selector", 8001, 5)
    selector._start_server = AsyncMock()
    selector._stop_server = AsyncMock()

    first = asyncio.create_task(selector.select("hash-one", [{"index": 0, "name": "a.bin"}]))
    second = asyncio.create_task(selector.select("hash-two", [{"index": 0, "name": "b.bin"}]))

    for _ in range(20):
        if selector.get("hash-one") and selector.get("hash-two"):
            break
        await asyncio.sleep(0.01)

    first_selection = selector.get("hash-one")
    second_selection = selector.get("hash-two")
    assert first_selection is not None
    assert second_selection is not None
    assert first_selection.token != second_selection.token

    first_selection.submitted.set()
    second_selection.submitted.set()

    assert await first == f"http://selector/select/{first_selection.token}"
    assert await second == f"http://selector/select/{second_selection.token}"
    assert selector.get("hash-one") is None
    assert selector.get("hash-two") is None
    selector._stop_server.assert_awaited_once()


def test_task_manager_prunes_old_terminal_tasks():
    manager = TaskManager.__new__(TaskManager)
    manager.tasks = {}
    for index in range(MAX_TERMINAL_TASKS + 5):
        task = make_task()
        task.created_at = index
        task.transition(TaskPhase.COMPLETE)
        manager.tasks[task.id] = task

    manager._prune_terminal_tasks()

    assert len(manager.tasks) == MAX_TERMINAL_TASKS
    assert min(task.created_at for task in manager.tasks.values()) == 5


@pytest.mark.asyncio
async def test_startup_removes_orphaned_qbittorrent_tasks():
    qb = SimpleNamespace(
        info=AsyncMock(
            return_value=[
                {"hash": "a" * 40, "name": "old-one"},
                {"hash": "b" * 40, "name": "old-two"},
            ]
        ),
        delete=AsyncMock(),
    )
    manager = TaskManager.__new__(TaskManager)
    manager.qb = qb

    removed = await manager.cleanup_orphaned_torrents(attempts=1)

    assert removed == 2
    assert qb.delete.await_count == 2
    qb.delete.assert_any_await("a" * 40, True)
    qb.delete.assert_any_await("b" * 40, True)


@pytest.mark.asyncio
async def test_dashboard_local_explorer_uses_owner_chat_id():
    class Explorer:
        def __init__(self):
            self.chat_id = None

        async def create(self, chat_id):
            self.chat_id = chat_id
            return "http://example.local/local/token"

    explorer = Explorer()
    dashboard = WebDashboard.__new__(WebDashboard)
    dashboard.config = SimpleNamespace(owner_id=12345)
    dashboard.file_explorer_getter = lambda: explorer

    response = await dashboard.api_local(None)

    assert isinstance(response, web.Response)
    assert explorer.chat_id == 12345
