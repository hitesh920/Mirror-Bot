import asyncio
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiohttp.client_exceptions import ClientResponseError

from mirrorbot.core.errors import (
    NetworkTimeoutError,
    TorrentDuplicateError,
    TorrentEngineError,
    TorrentMetadataTimeoutError,
    TorrentRemovedError,
)
from mirrorbot.core.config import Config
from mirrorbot.core.models import AddOptions, Destination, Source, SourceType, Task, TaskPhase
from mirrorbot.core.parser import parse_add_text, replied_link
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
from mirrorbot.services.page_style import TEMP_PAGE_CSS
from mirrorbot.services.google_drive_delivery import (
    DRIVE_CATEGORY_FOLDERS,
    FOLDER_MIME_TYPE,
    GoogleDriveUploader,
    ensure_drive_category_folders,
    next_drive_chunk,
)
from mirrorbot.services.task_manager import MAX_TERMINAL_TASKS, TaskManager
from mirrorbot.services.telegram_delivery import (
    telegram_chat_id,
    telegram_message_link,
    upload_files,
    upload_to_telegram,
)
from mirrorbot.telegram.messages import completion_message
from mirrorbot.telegram.keyboards import (
    destination_buttons,
    google_drive_folder_buttons,
)
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


def test_parse_magnet_with_spaces_and_hyphen_preserves_full_uri():
    magnet = (
        "magnet:?xt=urn:btih:abc123&dn=www.FiberMovies.com - Movie Name"
        "&xl=4733575398&tr=udp://tracker.example:6969/announce"
    )

    link, options = parse_add_text(f"/add {magnet}")

    assert link == magnet.replace(" ", "%20")
    assert options.extract is False
    assert options.zip is False


def test_parse_magnet_options_only_from_valid_suffix():
    magnet = "magnet:?xt=urn:btih:abc123&dn=Movie - Release Name"

    link, options = parse_add_text(f"/add {magnet} -e -zp secret")

    assert link == magnet.replace(" ", "%20")
    assert options.extract is True
    assert options.zip is True
    assert options.zip_password == "secret"


def test_replied_magnet_preserves_spaces_and_trackers():
    magnet = (
        "magnet:?xt=urn:btih:abc123&dn=Movie - Release Name"
        "&tr=udp://tracker.example:6969/announce"
    )

    assert replied_link(magnet) == magnet.replace(" ", "%20")


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


def test_google_drive_completion_mentions_category():
    task = make_task()
    task.destination = Destination.GOOGLE_DRIVE
    task.result_name = "movie.mkv"
    task.drive_folder_name = "Movies"

    text = completion_message(task)

    assert "Google Drive / Movies" in text


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
    assert "[hidden] { display: none !important; }" in TEMP_PAGE_CSS


def test_telegram_only_public_surface():
    assert {destination.value for destination in Destination} == {
        "telegram",
        "google_drive",
        "buzzheavier",
    }
    assert "local_path" not in {source.value for source in SourceType}
    labels = [
        button.text
        for row in destination_buttons("token").inline_keyboard
        for button in row
    ]
    assert labels == ["Telegram", "Google Drive", "BuzzHeavier"]

    removed_config = {
        "local_download_root",
        "jellyfin_api_key",
        "tmdb_api_key",
        "enable_web_ui",
        "web_port",
        "web_username",
        "web_password",
    }
    assert removed_config.isdisjoint(Config.__dataclass_fields__)


def test_google_drive_folder_keyboard_has_categories_and_back():
    keyboard = google_drive_folder_buttons("42")
    buttons = [button for row in keyboard.inline_keyboard for button in row]

    assert [button.text for button in buttons] == [
        "General",
        "Movies",
        "Series",
        "Games",
        "Back",
    ]
    assert [button.callback_data for button in buttons] == [
        "gdfolder:general:42",
        "gdfolder:movies:42",
        "gdfolder:series:42",
        "gdfolder:games:42",
        "gdfolder:back:42",
    ]


def test_google_drive_categories_are_public_and_idempotent(monkeypatch):
    folders = []
    public_ids = set()
    files_api = MagicMock()
    permissions_api = MagicMock()
    service = MagicMock()
    service.files.return_value = files_api
    service.permissions.return_value = permissions_api

    def list_files(**_kwargs):
        request = MagicMock()
        request.execute.side_effect = lambda: {"files": list(folders)}
        return request

    def create_file(body, **_kwargs):
        request = MagicMock()

        def execute():
            folder = {
                "id": f"folder-{len(folders) + 1}",
                "name": body["name"],
                "mimeType": FOLDER_MIME_TYPE,
                "createdTime": f"2026-01-0{len(folders) + 1}T00:00:00Z",
            }
            folders.append(folder)
            return folder

        request.execute.side_effect = execute
        return request

    def list_permissions(fileId, **_kwargs):
        request = MagicMock()
        request.execute.side_effect = lambda: {
            "permissions": (
                [{"id": "public", "type": "anyone", "role": "reader"}]
                if fileId in public_ids
                else []
            )
        }
        return request

    def create_permission(fileId, **_kwargs):
        request = MagicMock()
        request.execute.side_effect = lambda: public_ids.add(fileId) or {}
        return request

    files_api.list.side_effect = list_files
    files_api.create.side_effect = create_file
    permissions_api.list.side_effect = list_permissions
    permissions_api.create.side_effect = create_permission
    monkeypatch.setattr(gdrive_delivery, "drive_service", lambda _config: service)
    config = SimpleNamespace(google_drive_folder_id="root")

    first = ensure_drive_category_folders(config)
    second = ensure_drive_category_folders(config)

    assert set(first) == set(DRIVE_CATEGORY_FOLDERS)
    assert second == first
    assert [folder["name"] for folder in folders] == list(
        DRIVE_CATEGORY_FOLDERS.values()
    )
    assert public_ids == set(first.values())
    assert files_api.create.call_count == 4
    assert permissions_api.create.call_count == 4


def test_compose_exposes_only_temporary_page_ports():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert '"8001:8001"' in compose
    assert '"8002:8002"' in compose
    assert '"8003:8003"' in compose
    assert '"8000:8000"' not in compose
    assert '"8004:8004"' not in compose
    assert '"8005:8005"' not in compose
    assert "jellyfin:" not in compose
    assert "docker.sock" not in compose


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
    task.drive_folder_id = "category-folder"
    task.drive_folder_name = "General"

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
async def test_gdrive_upload_uses_selected_category_parent(tmp_path, monkeypatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"data")
    task = make_task()
    task.destination = Destination.GOOGLE_DRIVE
    task.drive_folder_id = "movies-folder"
    task.drive_folder_name = "Movies"

    uploader = GoogleDriveUploader.__new__(GoogleDriveUploader)
    uploader.task = task
    uploader.path = source
    uploader.config = SimpleNamespace(google_drive_folder_id="root")
    uploader.service = object()
    uploader.created_ids = []
    uploader.total_size = source.stat().st_size
    uploader.uploaded_base = 0
    uploader.started = 1
    uploader.upload_file = AsyncMock(return_value="uploaded-file")
    monkeypatch.setattr(
        gdrive_delivery,
        "unique_drive_name",
        lambda _service, _parent_id, name: name,
    )

    await uploader.upload()

    uploader.upload_file.assert_awaited_once_with(
        source,
        "movie.mkv",
        "movies-folder",
        "movie.mkv",
    )


@pytest.mark.asyncio
async def test_gdrive_upload_without_category_falls_back_to_general(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "file.bin"
    source.write_bytes(b"data")
    task = make_task()
    task.destination = Destination.GOOGLE_DRIVE

    uploader = GoogleDriveUploader.__new__(GoogleDriveUploader)
    uploader.task = task
    uploader.path = source
    uploader.config = SimpleNamespace(google_drive_folder_id="root")
    uploader.service = object()
    uploader.created_ids = []
    uploader.total_size = source.stat().st_size
    uploader.uploaded_base = 0
    uploader.started = 1
    uploader.upload_file = AsyncMock(return_value="uploaded-file")
    monkeypatch.setattr(
        gdrive_delivery,
        "ensure_drive_category_folders",
        lambda _config: {"general": "general-folder"},
    )
    monkeypatch.setattr(
        gdrive_delivery,
        "unique_drive_name",
        lambda _service, _parent_id, name: name,
    )

    await uploader.upload()

    assert task.drive_folder_id == "general-folder"
    assert task.drive_folder_name == "General"
    uploader.upload_file.assert_awaited_once_with(
        source,
        "file.bin",
        "general-folder",
        "file.bin",
    )


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
