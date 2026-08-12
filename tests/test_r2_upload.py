import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from mirrorbot.core.models import (
    AddOptions,
    Destination,
    Source,
    SourceType,
    Task,
)
from mirrorbot.services.r2 import upload as upload_module
from mirrorbot.services.r2.catalog import prepare_delete_all
from mirrorbot.services.r2.client import R2Service
from mirrorbot.services.r2.retention import delete_expired_objects
from mirrorbot.services.r2.upload import (
    R2Uploader,
    build_folder_page,
    folder_page_key,
    upload_metadata,
)


def make_task(tmp_path: Path) -> Task:
    return Task(
        id="task-id",
        user_id=1,
        chat_id=1,
        message_id=1,
        source=Source(SourceType.DIRECT_URL, "https://example.com/file.bin"),
        destination=Destination.CLOUDFLARE_R2,
        options=AddOptions(),
        work_dir=tmp_path,
    )


class FakeClient:
    def __init__(self, events: list[tuple], *, fail_key: str = ""):
        self.events = events
        self.fail_key = fail_key
        self.uploads: list[dict] = []
        self.parts: list[bytes] = []
        self.completed: list[dict] | None = None
        self.aborted: list[tuple[str, str]] = []

    def generate_presigned_url(self, _operation, *, Params, ExpiresIn):
        assert ExpiresIn == 604800
        return f"https://r2.example/{Params['Key']}"

    def put_object(self, **kwargs):
        key = kwargs["Key"]
        self.events.append(("put", key))
        if key == self.fail_key:
            raise RuntimeError("simulated upload failure")
        stored = dict(kwargs)
        body = stored["Body"]
        stored["Body"] = body.read() if hasattr(body, "read") else bytes(body)
        self.uploads.append(stored)
        return {}

    def create_multipart_upload(self, **kwargs):
        self.events.append(("multipart", kwargs["Key"]))
        return {"UploadId": "upload-id"}

    def upload_part(self, **kwargs):
        self.parts.append(bytes(kwargs["Body"]))
        return {"ETag": f"etag-{kwargs['PartNumber']}"}

    def complete_multipart_upload(self, **kwargs):
        self.completed = kwargs["MultipartUpload"]["Parts"]
        return {}

    def abort_multipart_upload(self, **kwargs):
        self.aborted.append((kwargs["Key"], kwargs["UploadId"]))
        return {}


class FakeService:
    def __init__(self, events: list[tuple], client: FakeClient | None = None):
        self.events = events
        self.config = SimpleNamespace(
            r2_prefix="uploads/",
            r2_bucket="mirror-bot",
            r2_auto_delete_seconds=172800,
        )
        self.bucket = self.config.r2_bucket
        self.client = client or FakeClient(events)
        self.active: set[str] = set()
        self.deleted: list[tuple[str, ...]] = []

    def validate_key(self, key: str) -> str:
        if not key.startswith("uploads/"):
            raise ValueError("outside prefix")
        return key

    def task_group(self, key: str) -> str:
        return "/".join(key.split("/")[:2]) + "/"

    def register_active_group(self, group: str) -> None:
        self.events.append(("register", group))
        self.active.add(group)

    def unregister_active_group(self, group: str) -> None:
        self.events.append(("unregister", group))
        self.active.discard(group)

    def _delete_validated_keys(self, keys) -> int:
        validated = tuple(self.validate_key(key) for key in keys)
        group = self.task_group(validated[0])
        self.events.append(("cleanup", group, group in self.active))
        self.deleted.append(validated)
        return len(validated)


@pytest.mark.asyncio
async def test_folder_upload_registers_before_writes_and_unregisters(tmp_path):
    source = tmp_path / "Season 1"
    source.mkdir()
    (source / "one.mkv").write_bytes(b"one")
    (source / "two.mkv").write_bytes(b"two")
    task = make_task(tmp_path)
    events: list[tuple] = []
    service = FakeService(events)

    await R2Uploader(task, source, service).upload()

    expected_page = folder_page_key(service.config, task, source.name)
    assert events[0] == ("register", "uploads/task-id/")
    assert events[-1] == ("unregister", "uploads/task-id/")
    assert service.active == set()
    assert task.result_links == [f"https://r2.example/{expected_page}"]
    assert task.result_files == ["Season 1/one.mkv", "Season 1/two.mkv"]
    assert task.result_is_folder is True
    page_upload = service.client.uploads[-1]
    assert page_upload["Key"] == expected_page
    assert page_upload["Metadata"]["mirror-kind"] == "folder"
    assert page_upload["Metadata"]["mirror-task"] == task.id
    assert b"one.mkv" in page_upload["Body"]
    assert b"Season 1/one.mkv</span>" not in page_upload["Body"]


@pytest.mark.asyncio
async def test_failed_upload_uses_private_cleanup_while_group_is_active(tmp_path):
    source = tmp_path / "files"
    source.mkdir()
    (source / "a.bin").write_bytes(b"a")
    (source / "b.bin").write_bytes(b"b")
    task = make_task(tmp_path)
    events: list[tuple] = []
    failed_key = "uploads/task-id/files/b.bin"
    client = FakeClient(events, fail_key=failed_key)
    service = FakeService(events, client)

    with pytest.raises(RuntimeError, match="simulated upload failure"):
        await R2Uploader(task, source, service).upload()

    assert service.deleted == [
        (
            "uploads/task-id/files/a.bin",
            "uploads/task-id/files/b.bin",
        )
    ]
    cleanup_index = next(i for i, event in enumerate(events) if event[0] == "cleanup")
    unregister_index = next(
        i for i, event in enumerate(events) if event[0] == "unregister"
    )
    assert events[cleanup_index] == ("cleanup", "uploads/task-id/", True)
    assert cleanup_index < unregister_index
    assert service.active == set()


@pytest.mark.asyncio
async def test_cancel_waits_for_sdk_call_then_cleans_and_unregisters(tmp_path):
    source = tmp_path / "movie.bin"
    source.write_bytes(b"payload")
    task = make_task(tmp_path)
    events: list[tuple] = []
    started = threading.Event()
    release = threading.Event()

    class BlockingClient(FakeClient):
        def put_object(self, **kwargs):
            started.set()
            assert release.wait(timeout=5)
            return super().put_object(**kwargs)

    service = FakeService(events, BlockingClient(events))
    operation = asyncio.create_task(R2Uploader(task, source, service).upload())
    assert await asyncio.to_thread(started.wait, 5)

    operation.cancel()
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done()
    assert not service.deleted
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation

    assert service.deleted == [("uploads/task-id/movie.bin",)]
    assert ("cleanup", "uploads/task-id/", True) in events
    assert events[-1] == ("unregister", "uploads/task-id/")
    assert service.active == set()


@pytest.mark.asyncio
async def test_cancelled_preflight_scan_drains_before_workspace_cleanup(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "movie.bin"
    source.write_bytes(b"payload")
    task = make_task(tmp_path)
    events: list[tuple] = []
    scan_started = threading.Event()
    finish_scan = threading.Event()
    original_prepare = upload_module._prepare_upload

    def blocking_prepare(path):
        scan_started.set()
        assert finish_scan.wait(timeout=5)
        return original_prepare(path)

    monkeypatch.setattr(upload_module, "_prepare_upload", blocking_prepare)
    operation = asyncio.create_task(
        R2Uploader(task, source, FakeService(events)).upload()
    )
    assert await asyncio.to_thread(scan_started.wait, 5)
    operation.cancel()
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done()
    finish_scan.set()

    with pytest.raises(asyncio.CancelledError):
        await operation

    assert not events


@pytest.mark.asyncio
async def test_repeated_cancel_cannot_interrupt_partial_object_cleanup(tmp_path):
    source = tmp_path / "movie.bin"
    source.write_bytes(b"payload")
    task = make_task(tmp_path)
    events: list[tuple] = []
    upload_started = threading.Event()
    finish_upload = threading.Event()
    cleanup_started = threading.Event()
    finish_cleanup = threading.Event()

    class BlockingClient(FakeClient):
        def put_object(self, **kwargs):
            upload_started.set()
            assert finish_upload.wait(timeout=5)
            return super().put_object(**kwargs)

    class BlockingCleanupService(FakeService):
        def _delete_validated_keys(self, keys):
            cleanup_started.set()
            assert finish_cleanup.wait(timeout=5)
            return super()._delete_validated_keys(keys)

    service = BlockingCleanupService(events, BlockingClient(events))
    operation = asyncio.create_task(R2Uploader(task, source, service).upload())
    assert await asyncio.to_thread(upload_started.wait, 5)
    operation.cancel()
    finish_upload.set()
    assert await asyncio.to_thread(cleanup_started.wait, 5)

    operation.cancel()
    await asyncio.sleep(0)
    assert service.active == {"uploads/task-id/"}
    finish_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await operation

    assert service.deleted == [("uploads/task-id/movie.bin",)]
    assert events[-1] == ("unregister", "uploads/task-id/")
    assert service.active == set()


@pytest.mark.asyncio
async def test_multipart_upload_reads_fixed_parts_and_completes(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "movie.bin"
    source.write_bytes(b"abcde")
    task = make_task(tmp_path)
    events: list[tuple] = []
    service = FakeService(events)
    uploader = R2Uploader(task, source, service)
    uploader.total_size = 5
    monkeypatch.setattr(upload_module, "MULTIPART_THRESHOLD", 1)
    monkeypatch.setattr(upload_module, "PART_SIZE", 2)

    await uploader.upload_file(
        source,
        "uploads/task-id/movie.bin",
        "https://r2.example/uploads/task-id/movie.bin",
        size=5,
    )

    assert service.client.parts == [b"ab", b"cd", b"e"]
    assert [part["PartNumber"] for part in service.client.completed] == [1, 2, 3]
    assert task.downloaded == 5
    assert task.progress == 1


@pytest.mark.asyncio
async def test_cancelled_multipart_creation_captures_and_aborts_upload_id(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "movie.bin"
    source.write_bytes(b"payload")
    task = make_task(tmp_path)
    events: list[tuple] = []
    create_started = threading.Event()
    finish_create = threading.Event()

    class BlockingCreateClient(FakeClient):
        def create_multipart_upload(self, **kwargs):
            create_started.set()
            assert finish_create.wait(timeout=5)
            return super().create_multipart_upload(**kwargs)

    service = FakeService(events, BlockingCreateClient(events))
    uploader = R2Uploader(task, source, service)
    uploader.total_size = 7
    monkeypatch.setattr(upload_module, "MULTIPART_THRESHOLD", 1)
    operation = asyncio.create_task(
        uploader.upload_file(
            source,
            "uploads/task-id/movie.bin",
            "https://r2.example/uploads/task-id/movie.bin",
            size=7,
        )
    )
    assert await asyncio.to_thread(create_started.wait, 5)
    operation.cancel()
    finish_create.set()

    with pytest.raises(asyncio.CancelledError):
        await operation

    assert service.client.aborted == [("uploads/task-id/movie.bin", "upload-id")]
    assert uploader.active_upload is None


@pytest.mark.asyncio
async def test_failed_multipart_abort_is_retried_by_outer_cleanup(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "movie.bin"
    source.write_bytes(b"payload")
    task = make_task(tmp_path)
    task.cancelled = True
    events: list[tuple] = []

    class FlakyAbortClient(FakeClient):
        def __init__(self, events):
            super().__init__(events)
            self.abort_attempts = 0

        def abort_multipart_upload(self, **kwargs):
            self.abort_attempts += 1
            if self.abort_attempts == 1:
                raise RuntimeError("transient abort failure")
            return super().abort_multipart_upload(**kwargs)

    client = FlakyAbortClient(events)
    service = FakeService(events, client)
    uploader = R2Uploader(task, source, service)
    uploader.total_size = 7
    monkeypatch.setattr(upload_module, "MULTIPART_THRESHOLD", 1)
    monkeypatch.setattr(upload_module, "PART_SIZE", 2)

    with pytest.raises(asyncio.CancelledError):
        await uploader.upload_file(
            source,
            "uploads/task-id/movie.bin",
            "https://r2.example/uploads/task-id/movie.bin",
            size=7,
        )
    assert uploader.active_upload is not None

    await uploader.cleanup_created("cancelled")

    assert client.abort_attempts == 2
    assert uploader.active_upload is None


def test_upload_metadata_keeps_task_kind_link_and_expiry(monkeypatch, tmp_path):
    task = make_task(tmp_path)
    config = SimpleNamespace(r2_auto_delete_seconds=3600)
    monkeypatch.setattr(upload_module, "time", lambda: 1_000)

    metadata = upload_metadata(config, task, "https://r2.example/file", "file")

    assert metadata == {
        "mirror-task": "task-id",
        "mirror-kind": "file",
        "mirror-link": "https://r2.example/file",
        "expires-at": "4600",
    }


def test_folder_page_escapes_names_and_preserves_download_links():
    page = build_folder_page(
        "Season <One>",
        [("nested/episode<&>.mkv", "https://r2.example/a?x=1&y=2", 1_000)],
        86400,
    ).decode()

    assert "Season &lt;One&gt;" in page
    assert "episode&lt;&amp;&gt;.mkv" in page
    assert "nested/episode" not in page
    assert "https://r2.example/a?x=1&amp;y=2" in page


@pytest.mark.asyncio
async def test_shared_registry_blocks_manual_and_expiry_deletion_during_upload(
    tmp_path,
):
    source = tmp_path / "movie.bin"
    source.write_bytes(b"payload")
    task = make_task(tmp_path)
    events: list[tuple] = []
    started = threading.Event()
    release = threading.Event()

    class Paginator:
        def __init__(self, client):
            self.client = client

        def paginate(self, **kwargs):
            yield {
                "Contents": [
                    item
                    for item in self.client.objects
                    if item["Key"].startswith(kwargs["Prefix"])
                ]
            }

    class BlockingClient(FakeClient):
        def __init__(self):
            super().__init__(events)
            self.objects = []

        def put_object(self, **kwargs):
            self.objects.append(
                {
                    "Key": kwargs["Key"],
                    "Size": 7,
                    "ETag": "uploading",
                    "LastModified": SimpleNamespace(timestamp=lambda: 1),
                }
            )
            started.set()
            assert release.wait(timeout=5)
            return super().put_object(**kwargs)

        def get_paginator(self, operation):
            assert operation == "list_objects_v2"
            return Paginator(self)

    client = BlockingClient()
    config = SimpleNamespace(
        r2_configured=True,
        r2_endpoint_url="https://r2.example",
        r2_bucket="mirror-bot",
        r2_access_key_id="key",
        r2_secret_access_key="secret",
        r2_prefix="uploads/",
        r2_auto_delete_seconds=1,
        task_limit=10,
    )
    service = R2Service(config, client)
    operation = asyncio.create_task(R2Uploader(task, source, service).upload())
    assert await asyncio.to_thread(started.wait, 5)

    plan = await asyncio.to_thread(prepare_delete_all, service)
    expired = await asyncio.to_thread(
        delete_expired_objects,
        service,
        objects=list(client.objects),
        now=10,
    )

    assert plan.keys == ()
    assert expired == 0
    assert service.active_groups == frozenset({"uploads/task-id/"})
    release.set()
    await operation
    assert not service.active_groups


def test_folder_page_reports_disabled_retention_truthfully():
    page = build_folder_page("No expiry", [], 0).decode()

    assert "Automatic deletion is disabled." in page
    assert "configured retention period" not in page


def test_folder_page_migration_skips_explicit_user_file_suffix():
    key = "uploads/task/user.mirrorbot-folder.html"

    class Body:
        def read(self):
            return (
                b'<li data-file-name="one" data-file-url="u">'
                b'<a href="u">Download</a><span>old</span><small>1</small>'
            )

        def close(self):
            return None

    class MigrationClient:
        def __init__(self):
            self.puts = []

        def get_object(self, **_kwargs):
            return {
                "Body": Body(),
                "Metadata": {"mirror-kind": "file"},
            }

        def put_object(self, **kwargs):
            self.puts.append(kwargs)

    client = MigrationClient()
    service = SimpleNamespace(
        client=client,
        bucket="mirror-bot",
        config=SimpleNamespace(r2_auto_delete_seconds=86400),
        list_objects=lambda: [{"Key": key}],
        validate_key=lambda value: value,
    )

    result = upload_module.update_existing_folder_pages(service)

    assert result == {"scanned": 1, "updated": 0, "labels": 0}
    assert not client.puts
