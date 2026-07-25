import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock
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
from mirrorbot.core.logging_config import sanitize_text
from mirrorbot.core.models import AddOptions, Destination, Source, SourceType, Task, TaskPhase
from mirrorbot.core.parser import parse_add_text, replied_link
from mirrorbot.core.source_detector import detect_source
from mirrorbot.downloaders.direct import retryable_direct_error
from mirrorbot.downloaders.torrent import selected_torrent_size
from mirrorbot.downloaders.torrent_selector import TorrentSelector
from mirrorbot.services import media_metadata
from mirrorbot.services import google_drive_delivery as gdrive_delivery
from mirrorbot.services import telegram_delivery
from mirrorbot.services import r2_delivery
from mirrorbot.services.buzzheavier_delivery import (
    buzzheavier_upload_name,
    duplicate_basenames,
)
from mirrorbot.services.page_style import TEMP_PAGE_CSS
from mirrorbot.services.google_drive_delivery import (
    GoogleDriveUploader,
    clear_drive_folder_contents,
    next_drive_chunk,
    search_drive_items,
)
from mirrorbot.services.r2_delivery import (
    R2Uploader,
    delete_expired_objects,
    key_from_input,
    normalize_prefix,
    object_key,
)
from mirrorbot.services.task_manager import MAX_TERMINAL_TASKS, TaskManager
from mirrorbot.services.telegram_delivery import (
    telegram_chat_id,
    telegram_message_link,
    upload_files,
    upload_to_telegram,
)
from mirrorbot.telegram.messages import completion_message
from mirrorbot.telegram.keyboards import destination_buttons
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


def test_google_drive_completion_mentions_destination():
    task = make_task()
    task.destination = Destination.GOOGLE_DRIVE
    task.result_name = "movie.mkv"

    text = completion_message(task)

    assert "Uploaded to:</b> <code>Google Drive" in text


def test_r2_completion_mentions_expiring_links():
    task = make_task()
    task.destination = Destination.CLOUDFLARE_R2
    task.result_name = "movie.mkv"
    task.result_files = ["movie.mkv"]
    task.result_links = ["https://example.r2/link"]

    text = completion_message(task)

    assert "Uploaded to:</b> <code>Cloudflare R2" in text
    assert "Links expire:</b> <code>24 hours" in text


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
        "cloudflare_r2",
    }
    assert "local_path" not in {source.value for source in SourceType}
    labels = [
        button.text
        for row in destination_buttons("token").inline_keyboard
        for button in row
    ]
    assert labels == [
        "Telegram",
        "Google Drive",
        "Cloudflare R2",
        "BuzzHeavier",
    ]

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


def test_presigned_r2_query_credentials_are_redacted():
    text = sanitize_text(
        "https://account.r2.cloudflarestorage.com/bucket/file?"
        "X-Amz-Credential=visible&X-Amz-Signature=secret"
    )

    assert "visible" not in text
    assert "secret" not in text
    assert text.count("REDACTED") == 2


def test_r2_prefix_and_task_key_are_scoped():
    task = make_task()
    config = SimpleNamespace(r2_prefix="/uploads/")

    assert normalize_prefix(config.r2_prefix) == "uploads/"
    assert (
        object_key(config, task, "../folder/movie.mkv")
        == f"uploads/{task.id}/folder/movie.mkv"
    )


def test_r2_key_input_accepts_signed_url_and_rejects_outside_prefix():
    config = SimpleNamespace(r2_bucket="mirror-bot", r2_prefix="uploads/")
    url = (
        "https://account.r2.cloudflarestorage.com/"
        "mirror-bot/uploads/task/movie%20name.mkv?X-Amz-Signature=secret"
    )

    assert key_from_input(config, url) == "uploads/task/movie name.mkv"
    with pytest.raises(ValueError, match="inside"):
        key_from_input(config, "other/file.bin")


def test_r2_expiry_only_deletes_old_objects(monkeypatch):
    now = datetime.now(timezone.utc)
    config = SimpleNamespace(r2_configured=True, r2_auto_delete_seconds=86400)
    objects = [
        {"Key": "uploads/old", "LastModified": now - timedelta(days=2)},
        {"Key": "uploads/new", "LastModified": now - timedelta(hours=2)},
    ]
    deleted = []
    monkeypatch.setattr(r2_delivery, "list_objects", lambda _config: objects)

    def fake_delete(_config, keys):
        deleted.extend(keys)
        return len(keys)

    monkeypatch.setattr(r2_delivery, "delete_keys", fake_delete)

    assert delete_expired_objects(config) == 1
    assert deleted == ["uploads/old"]


@pytest.mark.asyncio
async def test_r2_multipart_upload_tracks_parts_and_progress(tmp_path, monkeypatch):
    source = tmp_path / "movie.bin"
    source.write_bytes(b"abcde")
    task = make_task()
    task.destination = Destination.CLOUDFLARE_R2

    class Client:
        def __init__(self):
            self.parts = []
            self.completed = None

        def create_multipart_upload(self, **_kwargs):
            return {"UploadId": "upload-id"}

        def upload_part(self, **kwargs):
            self.parts.append(bytes(kwargs["Body"]))
            return {"ETag": f"etag-{kwargs['PartNumber']}"}

        def complete_multipart_upload(self, **kwargs):
            self.completed = kwargs["MultipartUpload"]["Parts"]
            return {}

        def abort_multipart_upload(self, **_kwargs):
            raise AssertionError("successful upload must not abort")

    client = Client()
    uploader = R2Uploader.__new__(R2Uploader)
    uploader.task = task
    uploader.path = source
    uploader.config = SimpleNamespace(
        r2_bucket="mirror-bot",
        r2_auto_delete_seconds=86400,
    )
    uploader.client = client
    uploader.created_keys = ["uploads/task/movie.bin"]
    uploader.active_upload = None
    uploader.total_size = source.stat().st_size
    uploader.uploaded = 0
    uploader.started = 1
    monkeypatch.setattr(r2_delivery, "MULTIPART_THRESHOLD", 1)
    monkeypatch.setattr(r2_delivery, "PART_SIZE", 2)

    await uploader.upload_file(source, "uploads/task/movie.bin")

    assert client.parts == [b"ab", b"cd", b"e"]
    assert [part["PartNumber"] for part in client.completed] == [1, 2, 3]
    assert task.downloaded == 5
    assert task.progress == 1


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
async def test_gdrive_upload_uses_configured_folder_parent(tmp_path, monkeypatch):
    source = tmp_path / "movie.mkv"
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
        "unique_drive_name",
        lambda _service, _parent_id, name: name,
    )

    await uploader.upload()

    uploader.upload_file.assert_awaited_once_with(
        source,
        "movie.mkv",
        "root",
        "movie.mkv",
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


def test_clear_drive_folder_contents_preserves_configured_root(monkeypatch):
    deleted_ids = []

    class DeleteRequest:
        def __init__(self, file_id):
            self.file_id = file_id

        def execute(self):
            deleted_ids.append(self.file_id)

    class Files:
        def delete(self, *, fileId, supportsAllDrives):
            assert supportsAllDrives is True
            return DeleteRequest(fileId)

    service = SimpleNamespace(files=lambda: Files())
    children = [
        {"id": "file-id", "name": "file.bin"},
        {"id": "folder-id", "name": "Folder"},
    ]
    monkeypatch.setattr(gdrive_delivery, "drive_service", lambda _config: service)
    monkeypatch.setattr(
        gdrive_delivery,
        "drive_folder_children",
        lambda _service, folder_id: children if folder_id == "root" else [],
    )

    result = clear_drive_folder_contents(
        SimpleNamespace(google_drive_folder_id="root")
    )

    assert deleted_ids == ["file-id", "folder-id"]
    assert "root" not in deleted_ids
    assert result == {
        "deleted": 2,
        "failed": [],
    }


def test_drive_search_is_recursive_and_scoped_to_configured_root(monkeypatch):
    folder = "application/vnd.google-apps.folder"
    children = {
        "configured-root": [
            {"id": "nested", "name": "Projects", "mimeType": folder},
            {"id": "root-file", "name": "notes.txt", "mimeType": "text/plain"},
        ],
        "nested": [
            {"id": "match", "name": ".dart_tool", "mimeType": folder},
            {"id": "not-match", "name": "dart_style", "mimeType": folder},
        ],
        "match": [],
        "not-match": [],
        "outside-root": [
            {"id": "outside", "name": ".dart_cache", "mimeType": folder},
        ],
    }
    visited = []
    monkeypatch.setattr(gdrive_delivery, "drive_service", lambda _config: object())

    def folder_children(_service, folder_id):
        visited.append(folder_id)
        return children[folder_id]

    monkeypatch.setattr(gdrive_delivery, "drive_folder_children", folder_children)

    results = search_drive_items(
        SimpleNamespace(google_drive_folder_id="configured-root"),
        ".dart",
    )

    assert [item["id"] for item in results] == ["match"]
    assert "outside-root" not in visited
    assert set(visited) == {"configured-root", "nested", "match", "not-match"}


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
