import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from mirrorbot.services.r2.retention import (
    MAX_FOLDER_PAGE_DISCOVERY_HEADS,
    delete_expired_objects,
    expiring_uploads,
    expiry_warning_token,
    load_warning_state,
    run_expiry_sweep_once,
)

NOW = 2_000_000_000


def item(key, modified, size=1, etag="etag"):
    return {
        "Key": key,
        "Size": size,
        "ETag": etag,
        "LastModified": datetime.fromtimestamp(modified, UTC),
    }


class FakeService:
    prefix = "uploads/"

    def __init__(self, objects, metadata, retention=86_400):
        self.config = SimpleNamespace(r2_auto_delete_seconds=retention)
        self.objects = objects
        self.metadata = metadata
        self.active = set()
        self.deleted = []
        self.metadata_calls = []

    def validate_key(self, key):
        assert key.startswith(self.prefix) and key != self.prefix
        return key

    def task_group(self, key):
        return key.removeprefix(self.prefix).split("/", 1)[0]

    def is_group_active(self, group):
        return group in self.active

    def metadata_for(self, stored):
        self.metadata_calls.append(stored["Key"])
        value = self.metadata[stored["Key"]]
        if isinstance(value, Exception):
            raise value
        return value

    def list_objects(self):
        return self.objects

    def prune_metadata_cache(self, _objects):
        return None

    def delete_keys(self, keys):
        self.deleted.extend(keys)
        return len(keys)


def test_folder_page_stored_expiry_controls_complete_group():
    page = item(
        "uploads/task/Folder.mirrorbot-folder.html",
        NOW,
        etag="page",
    )
    child = item("uploads/task/Folder/new.bin", NOW + 1000, size=20)
    service = FakeService(
        [page, child],
        {
            page["Key"]: {
                "mirror-kind": "folder",
                "expires-at": str(NOW - 1),
            },
            child["Key"]: {
                "mirror-kind": "file",
                "expires-at": str(NOW + 10_000),
            },
        },
    )

    assert delete_expired_objects(service, objects=service.objects, now=NOW) == 2
    assert service.deleted == [page["Key"], child["Key"]]


def test_future_folder_page_preserves_old_children():
    page = item("uploads/task/Folder.mirrorbot-folder.html", NOW - 200_000)
    child = item("uploads/task/Folder/old.bin", NOW - 200_000)
    service = FakeService(
        [page, child],
        {
            page["Key"]: {
                "mirror-kind": "folder",
                "expires-at": str(NOW + 100),
            },
            child["Key"]: {"mirror-kind": "file"},
        },
    )

    assert delete_expired_objects(service, objects=service.objects, now=NOW) == 0
    assert not service.deleted


def test_legacy_folder_page_uses_page_modified_for_complete_group():
    page = item("uploads/task/Folder.mirrorbot-folder.html", NOW - 86_401)
    child = item("uploads/task/Folder/new.bin", NOW, size=20)
    service = FakeService(
        [page, child],
        {
            page["Key"]: {},
            child["Key"]: {},
        },
    )

    assert delete_expired_objects(service, objects=service.objects, now=NOW) == 2


def test_metadata_folder_page_does_not_head_a_failing_child():
    page = item("uploads/task/index.html", NOW - 86_401, etag="page")
    child = item("uploads/task/Folder/child.bin", NOW, size=20, etag="child")
    service = FakeService(
        [child, page],
        {
            page["Key"]: {"mirror-kind": "folder"},
            child["Key"]: RuntimeError("child HEAD failed"),
        },
    )

    assert delete_expired_objects(service, objects=service.objects, now=NOW) == 2
    assert service.deleted == [child["Key"], page["Key"]]
    assert service.metadata_calls == [page["Key"]]


def test_folder_discovery_is_bounded_and_skips_an_unproven_group():
    files = [
        item(f"uploads/task/a-{index:02d}.bin", NOW - 86_401, etag=str(index))
        for index in range(MAX_FOLDER_PAGE_DISCOVERY_HEADS)
    ]
    hidden_page = item("uploads/task/z-page.bin", NOW - 86_401, etag="page")
    objects = [*files, hidden_page]
    service = FakeService(
        objects,
        {
            **{stored["Key"]: {"mirror-kind": "file"} for stored in files},
            hidden_page["Key"]: {"mirror-kind": "folder"},
        },
    )

    assert delete_expired_objects(service, objects=objects, now=NOW) == 0
    assert len(service.metadata_calls) == MAX_FOLDER_PAGE_DISCOVERY_HEADS
    assert hidden_page["Key"] not in service.metadata_calls


def test_explicit_file_kind_prevents_folder_suffix_group_expiry():
    suffix_file = item(
        "uploads/task/user.mirrorbot-folder.html",
        NOW,
        size=5,
    )
    valuable = item("uploads/task/valuable.bin", NOW, size=500)
    service = FakeService(
        [suffix_file, valuable],
        {
            suffix_file["Key"]: {
                "mirror-kind": "file",
                "expires-at": str(NOW - 1),
            },
            valuable["Key"]: {
                "mirror-kind": "file",
                "expires-at": str(NOW + 10_000),
            },
        },
    )

    assert delete_expired_objects(service, objects=service.objects, now=NOW) == 1
    assert service.deleted == [suffix_file["Key"]]


def test_standalone_files_use_individual_stored_expiry():
    expired = item("uploads/task/old.bin", NOW, etag="old")
    future = item("uploads/task/future.bin", NOW - 200_000, etag="future")
    service = FakeService(
        [expired, future],
        {
            expired["Key"]: {"expires-at": str(NOW - 1)},
            future["Key"]: {"expires-at": str(NOW + 1)},
        },
    )

    assert delete_expired_objects(service, objects=service.objects, now=NOW) == 1
    assert service.deleted == [expired["Key"]]


def test_active_group_is_skipped_for_warning_and_deletion():
    stored = item("uploads/task/file.bin", NOW - 100_000)
    service = FakeService(
        [stored],
        {stored["Key"]: {"expires-at": str(NOW - 1)}},
    )
    service.active.add("task")

    assert not expiring_uploads(service, objects=service.objects, now=NOW - 100)
    assert delete_expired_objects(service, objects=service.objects, now=NOW) == 0


def test_metadata_failure_conservatively_skips_group():
    stored = item("uploads/task/file.bin", NOW - 200_000)
    service = FakeService(
        [stored],
        {stored["Key"]: RuntimeError("head failed")},
    )

    assert delete_expired_objects(service, objects=service.objects, now=NOW) == 0


def test_invalid_folder_expiry_conservatively_preserves_complete_group():
    page = item("uploads/task/Folder.mirrorbot-folder.html", NOW - 200_000)
    child = item("uploads/task/Folder/old.bin", NOW - 200_000)
    service = FakeService(
        [page, child],
        {
            page["Key"]: {"mirror-kind": "folder", "expires-at": "invalid"},
            child["Key"]: {"mirror-kind": "file"},
        },
    )

    assert delete_expired_objects(service, objects=service.objects, now=NOW) == 0
    assert not service.deleted


def test_invalid_standalone_expiry_is_not_replaced_by_legacy_fallback():
    invalid = item("uploads/task/invalid.bin", NOW - 200_000)
    expired = item("uploads/task/expired.bin", NOW - 200_000)
    service = FakeService(
        [invalid, expired],
        {
            invalid["Key"]: {"mirror-kind": "file", "expires-at": "invalid"},
            expired["Key"]: {"mirror-kind": "file"},
        },
    )

    assert delete_expired_objects(service, objects=service.objects, now=NOW) == 1
    assert service.deleted == [expired["Key"]]


def test_warning_uses_same_stored_expiry_as_deletion_plan():
    stored = item("uploads/task/file.bin", NOW)
    service = FakeService(
        [stored],
        {stored["Key"]: {"expires-at": str(NOW + 3600)}},
    )

    warnings = expiring_uploads(service, objects=service.objects, now=NOW)

    assert len(warnings) == 1
    assert warnings[0]["expires_at"] == NOW + 3600
    assert warnings[0]["remaining_seconds"] == 3600


def test_standalone_warning_tokens_do_not_collide_within_a_task_group():
    common = {"group": "task", "expires_at": NOW + 3600}

    assert expiry_warning_token({**common, "key": "uploads/task/one.bin"}) != (
        expiry_warning_token({**common, "key": "uploads/task/two.bin"})
    )


@pytest.mark.asyncio
async def test_failed_warning_is_retried_and_does_not_block_expired_deletion(
    tmp_path,
    monkeypatch,
):
    soon = item("uploads/soon/file.bin", NOW)
    expired = item("uploads/expired/file.bin", NOW)
    service = FakeService(
        [soon, expired],
        {
            soon["Key"]: {"expires-at": str(NOW + 3600)},
            expired["Key"]: {"expires-at": str(NOW - 1)},
        },
    )
    state = {}
    warning_attempts = []

    class FixedDateTime:
        @staticmethod
        def now(_timezone):
            return datetime.fromtimestamp(NOW, UTC)

    async def fail_warning(upload):
        warning_attempts.append(upload["key"])
        raise RuntimeError("telegram unavailable")

    monkeypatch.setattr(
        "mirrorbot.services.r2.retention.datetime",
        FixedDateTime,
    )

    first = await run_expiry_sweep_once(
        service,
        fail_warning,
        state,
        tmp_path / "warnings.json",
    )
    second = await run_expiry_sweep_once(
        service,
        fail_warning,
        state,
        tmp_path / "warnings.json",
    )

    assert first == {"warned": 0, "deleted": 1}
    assert second == {"warned": 0, "deleted": 1}
    assert service.deleted == [expired["Key"], expired["Key"]]
    assert warning_attempts == [soon["Key"], soon["Key"]]
    assert state == {}


@pytest.mark.asyncio
async def test_hung_warning_is_bounded_after_expired_deletion(tmp_path, monkeypatch):
    soon = item("uploads/soon/file.bin", NOW)
    expired = item("uploads/expired/file.bin", NOW)
    service = FakeService(
        [soon, expired],
        {
            soon["Key"]: {"expires-at": str(NOW + 3600)},
            expired["Key"]: {"expires-at": str(NOW - 1)},
        },
    )
    notification_started = asyncio.Event()

    class FixedDateTime:
        @staticmethod
        def now(_timezone):
            return datetime.fromtimestamp(NOW, UTC)

    async def hang_forever(_upload):
        assert service.deleted == [expired["Key"]]
        notification_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("mirrorbot.services.r2.retention.datetime", FixedDateTime)
    monkeypatch.setattr(
        "mirrorbot.services.r2.retention.WARNING_NOTIFICATION_TIMEOUT",
        0.01,
    )

    result = await asyncio.wait_for(
        run_expiry_sweep_once(
            service,
            hang_forever,
            {},
            tmp_path / "warnings.json",
        ),
        timeout=1,
    )

    assert notification_started.is_set()
    assert result == {"warned": 0, "deleted": 1}


@pytest.mark.asyncio
async def test_cancellation_persists_earlier_successful_warning(tmp_path, monkeypatch):
    first = item("uploads/first/file.bin", NOW, etag="first")
    second = item("uploads/second/file.bin", NOW, etag="second")
    service = FakeService(
        [first, second],
        {
            first["Key"]: {"expires-at": str(NOW + 100)},
            second["Key"]: {"expires-at": str(NOW + 200)},
        },
    )
    state = {}
    state_path = tmp_path / "warnings.json"
    second_started = asyncio.Event()

    class FixedDateTime:
        @staticmethod
        def now(_timezone):
            return datetime.fromtimestamp(NOW, UTC)

    async def notify(upload):
        if upload["key"] == first["Key"]:
            return
        second_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("mirrorbot.services.r2.retention.datetime", FixedDateTime)

    sweep = asyncio.create_task(
        run_expiry_sweep_once(service, notify, state, state_path)
    )
    await asyncio.wait_for(second_started.wait(), timeout=1)
    sweep.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sweep

    first_upload = {
        "key": first["Key"],
        "expires_at": NOW + 100,
    }
    token = expiry_warning_token(first_upload)
    expected = {token: NOW + 100}
    assert state == expected
    assert load_warning_state(state_path) == expected
