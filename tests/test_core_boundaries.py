import asyncio
import os
from datetime import datetime, timedelta, timezone
from email.header import Header
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiohttp.client_exceptions import ClientResponseError

from mirrorbot.core.config import Config
from mirrorbot.core.errors import (
    NetworkTimeoutError,
    TorrentDuplicateError,
    TorrentEngineError,
    TorrentMetadataTimeoutError,
    TorrentRemovedError,
)
from mirrorbot.core.formatting import human_size
from mirrorbot.core.logging_config import sanitize_text
from mirrorbot.core.models import (
    AddOptions,
    Destination,
    Source,
    SourceType,
    Task,
    TaskPhase,
)
from mirrorbot.core.parser import parse_add_text, replied_link
from mirrorbot.core.source_detector import detect_source
from mirrorbot.downloaders.direct import retryable_direct_error
from mirrorbot.downloaders.torrent import selected_torrent_size
from mirrorbot.downloaders.torrent_selector import TorrentSelector
from mirrorbot.services import media_metadata, r2_delivery, telegram_delivery
from mirrorbot.services.cloudflare_analytics import (
    billing_period,
    classify_operations,
)
from mirrorbot.services.page_style import TEMP_PAGE_CSS
from mirrorbot.services.r2_delivery import (
    R2Uploader,
    build_folder_page,
    decode_metadata_value,
    delete_expired_objects,
    delete_scope,
    folder_page_key,
    key_from_input,
    normalize_prefix,
    object_key,
    search_objects,
)
from mirrorbot.services.task_manager import MAX_TERMINAL_TASKS, TaskManager
from mirrorbot.services.telegram_delivery import (
    telegram_chat_id,
    telegram_message_link,
    upload_files,
    upload_to_telegram,
)
from mirrorbot.telegram.keyboards import completion_buttons, destination_buttons
from mirrorbot.telegram.messages import HELP_TEXT, completion_message
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


def test_r2_completion_uses_task_retention_without_link_expiry():
    task = make_task()
    task.destination = Destination.CLOUDFLARE_R2
    task.result_name = "movie.mkv"
    task.result_files = ["movie.mkv"]
    task.result_links = ["https://example.r2/link"]
    task.result_auto_delete_seconds = 172800

    text = completion_message(task)

    assert "Uploaded to:</b> <code>Cloudflare R2" in text
    assert "Automatically deleted after:</b> <code>2 days" in text
    assert "expire" not in text.casefold()
    buttons = completion_buttons(task).inline_keyboard
    assert [[button.text for button in row] for row in buttons] == [["Download"]]


def test_cloudflare_operation_classes_follow_r2_pricing():
    groups = [
        {
            "dimensions": {"actionType": "UploadPart"},
            "sum": {"requests": 20},
        },
        {
            "dimensions": {"actionType": "ListObjects"},
            "sum": {"requests": 3},
        },
        {
            "dimensions": {"actionType": "GetObject"},
            "sum": {"requests": 7},
        },
        {
            "dimensions": {"actionType": "DeleteObjects"},
            "sum": {"requests": 99},
        },
    ]

    assert classify_operations(groups) == (23, 7)


def test_cloudflare_billing_period_uses_monthly_anchor():
    from datetime import datetime, timezone

    anchor = datetime(2026, 8, 25, tzinfo=timezone.utc)
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    start = datetime(2026, 7, 25, tzinfo=timezone.utc)

    assert billing_period(anchor, now, start) == (
        start,
        datetime(2026, 8, 25, tzinfo=timezone.utc),
    )


def test_r2_uses_unprefixed_search_and_delete_commands():
    source = Path("mirrorbot/commands/r2.py").read_text(encoding="utf-8")

    assert 'filters.command("search")' in source
    assert 'filters.command("delete")' in source
    assert 'filters.command("r2search")' not in source
    assert 'filters.command("r2delete")' not in source
    assert "/search" in HELP_TEXT
    assert "/delete" in HELP_TEXT
    assert "/r2search" not in HELP_TEXT
    assert "/r2delete" not in HELP_TEXT


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
        "Cloudflare R2",
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


def test_folder_page_contains_every_original_file_link():
    page = build_folder_page(
        "Season 1",
        [
            ("Season 1/one.mkv", "https://r2.example/one", 10),
            ("Season 1/two.mkv", "https://r2.example/two", 20),
        ],
        172800,
    ).decode()

    assert "Season 1/one.mkv" in page
    assert "Season 1/two.mkv" in page
    assert "https://r2.example/one" in page
    assert "https://r2.example/two" in page
    assert "Automatically deleted after 2 days" in page


def test_folder_page_displays_adaptive_decimal_file_sizes():
    page = build_folder_page(
        "Mixed sizes",
        [
            ("small.txt", "https://r2.example/small", 999),
            ("kilobytes.bin", "https://r2.example/kilobytes", 1_500),
            ("megabytes.bin", "https://r2.example/megabytes", 711_052_006),
            ("gigabytes.bin", "https://r2.example/gigabytes", 3_400_000_000),
        ],
        172800,
    ).decode()

    assert "<small>999 B</small>" in page
    assert "<small>1.5 KB</small>" in page
    assert "<small>711.1 MB</small>" in page
    assert "<small>3.4 GB</small>" in page
    assert "711,052,006 bytes" not in page


def test_human_size_uses_decimal_units_everywhere():
    assert human_size(999) == "999 B"
    assert human_size(1_500) == "1.5 KB"
    assert human_size(711_052_006) == "711.1 MB"
    assert human_size(6_300_000_000) == "6.3 GB"
    assert human_size(7_100_000_000_000) == "7.1 TB"


def test_folder_page_copy_all_uses_basenames_and_original_links():
    page = build_folder_page(
        "Season 1",
        [
            (
                "Season 1/Episodes/one.mkv",
                "https://r2.example/one?signature=1&download=yes",
                10,
            ),
            (
                r"Season 1\Extras\two.mkv",
                "https://r2.example/two",
                20,
            ),
        ],
        172800,
    ).decode()

    assert '<button id="copy-all" type="button">Copy all</button>' in page
    assert 'data-file-name="one.mkv"' in page
    assert 'data-file-name="two.mkv"' in page
    assert 'data-file-name="Season 1/Episodes/one.mkv"' not in page
    assert "item.dataset.fileName" in page
    assert "item.dataset.fileUrl" in page
    assert (
        'data-file-url="https://r2.example/one?signature=1&amp;download=yes"'
        in page
    )


def test_r2_search_returns_stored_original_link_without_presigning(monkeypatch):
    config = SimpleNamespace(r2_bucket="mirror-bot")
    objects = [
        {
            "Key": "uploads/task/movie.mkv",
            "Size": 100,
        }
    ]

    class Client:
        def head_object(self, **_kwargs):
            return {
                "Metadata": {
                    "mirror-link": "https://original.example/link",
                    "mirror-kind": "file",
                }
            }

        def generate_presigned_url(self, *_args, **_kwargs):
            raise AssertionError("/search must not create a new link")

    monkeypatch.setattr(r2_delivery, "list_objects", lambda _config: objects)
    monkeypatch.setattr(r2_delivery, "r2_client", lambda _config: Client())

    results = search_objects(config, "movie")

    assert results[0]["url"] == "https://original.example/link"


def test_r2_metadata_link_decoding_does_not_insert_fold_spaces():
    url = (
        "https://account.r2.cloudflarestorage.com/bucket/file?"
        + "X-Amz-Signature="
        + ("a" * 300)
    )
    encoded = Header(url, "utf-8", maxlinelen=76).encode()

    assert decode_metadata_value(encoded) == url


def test_folder_delete_scope_includes_every_task_object(monkeypatch):
    config = SimpleNamespace(r2_prefix="uploads/")
    key = "uploads/task/Season 1.mirrorbot-folder.html"
    objects = [
        {"Key": key, "Size": 200},
        {"Key": "uploads/task/Season 1/one.mkv", "Size": 10},
        {"Key": "uploads/task/Season 1/two.mkv", "Size": 20},
    ]
    monkeypatch.setattr(
        r2_delivery,
        "list_objects",
        lambda _config, prefix=None: objects
        if prefix == "uploads/task/"
        else [],
    )

    result = delete_scope(config, key)

    assert result["kind"] == "folder"
    assert result["objects"] == 3
    assert result["bytes"] == 230
    assert result["keys"] == [item["Key"] for item in objects]


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

    await uploader.upload_file(
        source,
        "uploads/task/movie.bin",
        "https://original.example/movie",
    )

    assert client.parts == [b"ab", b"cd", b"e"]
    assert [part["PartNumber"] for part in client.completed] == [1, 2, 3]
    assert task.downloaded == 5
    assert task.progress == 1


@pytest.mark.asyncio
async def test_r2_folder_upload_returns_one_folder_page_link(tmp_path, monkeypatch):
    source = tmp_path / "Season 1"
    source.mkdir()
    (source / "one.mkv").write_bytes(b"one")
    (source / "two.mkv").write_bytes(b"two")
    task = make_task()
    task.destination = Destination.CLOUDFLARE_R2

    class Client:
        def __init__(self):
            self.uploads = []

        def generate_presigned_url(self, _operation, Params, ExpiresIn):
            assert ExpiresIn == 604800
            return f"https://r2.example/{Params['Key']}"

        def put_object(self, **kwargs):
            self.uploads.append(kwargs)
            return {}

    client = Client()
    config = SimpleNamespace(
        r2_configured=True,
        r2_bucket="mirror-bot",
        r2_prefix="uploads/",
        r2_auto_delete_seconds=172800,
    )
    monkeypatch.setattr(r2_delivery, "r2_client", lambda _config: client)

    uploader = R2Uploader(task, source, config)
    await uploader.upload()

    expected_page_key = folder_page_key(config, task, "Season 1")
    assert task.result_is_folder is True
    assert task.result_auto_delete_seconds == 172800
    assert task.result_links == [f"https://r2.example/{expected_page_key}"]
    assert len(task.result_files) == 2
    page_upload = client.uploads[-1]
    assert page_upload["Key"] == expected_page_key
    assert page_upload["ContentDisposition"] == "inline"
    assert page_upload["Metadata"]["mirror-kind"] == "folder"
    page = page_upload["Body"].decode()
    assert "one.mkv" in page
    assert "two.mkv" in page


def test_compose_exposes_only_temporary_page_ports():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert '"8001:8001"' in compose
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
