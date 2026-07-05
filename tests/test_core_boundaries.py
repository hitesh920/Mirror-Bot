import asyncio
from pathlib import Path
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
from mirrorbot.downloaders.torrent_selector import TorrentSelector
from mirrorbot.services import media_metadata
from mirrorbot.services import google_drive_delivery as gdrive_delivery
from mirrorbot.services.google_drive_delivery import GoogleDriveUploader
from mirrorbot.services.task_manager import MAX_TERMINAL_TASKS, TaskManager
from mirrorbot.services.telegram_delivery import telegram_chat_id, telegram_message_link
from mirrorbot.services.web.auth import credentials_match, is_public_path
from mirrorbot.services.web_dashboard import WebDashboard
from mirrorbot.telegram.messages import completion_message
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
