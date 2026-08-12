import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from mirrorbot.core import config as config_module
from mirrorbot.core.config import Config
from mirrorbot.services.r2.client import R2Service, normalize_prefix


class FakePaginator:
    def __init__(self, owner):
        self.owner = owner

    def paginate(self, **kwargs):
        self.owner.list_calls.append(kwargs)
        prefix = kwargs["Prefix"]
        yield {
            "Contents": [
                item for item in self.owner.objects if item["Key"].startswith(prefix)
            ]
        }


class FakeClient:
    def __init__(self):
        self.objects = []
        self.metadata = {}
        self.list_calls = []
        self.head_calls = []
        self.delete_calls = []
        self.close_calls = 0

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return FakePaginator(self)

    def head_object(self, **kwargs):
        self.head_calls.append(kwargs)
        return {"Metadata": self.metadata[kwargs["Key"]]}

    def delete_objects(self, **kwargs):
        self.delete_calls.append(kwargs)
        return {"Deleted": kwargs["Delete"]["Objects"]}

    def close(self):
        self.close_calls += 1


def make_config(prefix="uploads/"):
    return SimpleNamespace(
        r2_configured=True,
        r2_endpoint_url="https://r2.example",
        r2_bucket="bucket",
        r2_access_key_id="key",
        r2_secret_access_key="secret",
        r2_prefix=prefix,
        task_limit=10,
    )


def listed(key, etag="one", modified=1, size=10):
    return {
        "Key": key,
        "ETag": etag,
        "LastModified": datetime.fromtimestamp(modified, UTC),
        "Size": size,
    }


def test_prefix_normalization_defaults_blank_and_rejects_root():
    assert normalize_prefix("") == "uploads/"
    assert normalize_prefix(" /nested/path/ ") == "nested/path/"
    for value in ("/", "///", "a//b", "../escape", r"a\b"):
        with pytest.raises(ValueError):
            normalize_prefix(value)


def test_config_blank_prefix_defaults_and_task_limit_is_bounded(monkeypatch):
    monkeypatch.setattr(config_module, "load_dotenv", lambda: None)
    for key, value in {
        "BOT_TOKEN": "token",
        "OWNER_ID": "1",
        "TELEGRAM_API_ID": "2",
        "TELEGRAM_API_HASH": "hash",
        "R2_PREFIX": "   ",
        "TASK_LIMIT": "50",
    }.items():
        monkeypatch.setenv(key, value)

    loaded = Config.load()

    assert loaded.r2_prefix == "uploads/"
    assert loaded.task_limit == 50
    monkeypatch.setenv("TASK_LIMIT", "51")
    with pytest.raises(RuntimeError, match="between 1 and 50"):
        Config.load()


def test_config_rejects_root_r2_prefix(monkeypatch):
    monkeypatch.setattr(config_module, "load_dotenv", lambda: None)
    for key, value in {
        "BOT_TOKEN": "token",
        "OWNER_ID": "1",
        "TELEGRAM_API_ID": "2",
        "TELEGRAM_API_HASH": "hash",
        "R2_ENDPOINT_URL": "https://r2.example",
        "R2_BUCKET": "bucket",
        "R2_ACCESS_KEY_ID": "key",
        "R2_SECRET_ACCESS_KEY": "secret",
        "R2_PREFIX": "/",
    }.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(RuntimeError, match="bucket root"):
        Config.load()


def test_config_resolves_disabled_root_r2_prefix_to_safe_default(monkeypatch):
    monkeypatch.setattr(config_module, "load_dotenv", lambda: None)
    for key, value in {
        "BOT_TOKEN": "token",
        "OWNER_ID": "1",
        "TELEGRAM_API_ID": "2",
        "TELEGRAM_API_HASH": "hash",
        "R2_PREFIX": "/",
    }.items():
        monkeypatch.setenv(key, value)
    for key in (
        "R2_ENDPOINT_URL",
        "R2_BUCKET",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    loaded = Config.load()

    assert not loaded.r2_configured
    assert loaded.r2_prefix == "uploads/"


def test_delete_validates_complete_batch_before_s3_call():
    client = FakeClient()
    service = R2Service(make_config(), client)

    with pytest.raises(ValueError):
        service.delete_keys(["uploads/task/file", "outside/file"])

    assert not client.delete_calls


def test_root_and_outside_list_scopes_are_rejected_without_s3_call():
    client = FakeClient()
    service = R2Service(make_config(), client)

    for prefix in ("", "/", "outside/", "uploads/../outside/"):
        with pytest.raises(ValueError):
            service.list_objects(prefix)

    assert not client.list_calls


def test_active_group_is_skipped_by_external_delete_but_private_cleanup_works():
    client = FakeClient()
    service = R2Service(make_config(), client)
    group = "uploads/task/"
    key = f"{group}file.bin"
    service.register_active_group(group)

    assert service.delete_keys([key]) == 0
    assert not client.delete_calls
    assert service._delete_validated_keys([key]) == 1
    assert len(client.delete_calls) == 1


def test_active_group_registry_uses_reference_counts():
    service = R2Service(make_config(), FakeClient())
    group = "uploads/task/"

    service.register_active_group(group)
    service.register_active_group(group)
    service.unregister_active_group(group)
    assert service.is_group_active(group)
    service.unregister_active_group(group)
    assert not service.is_group_active(group)


def test_delete_reservation_prevents_upload_start_race():
    entered = threading.Event()
    release = threading.Event()

    class BlockingDeleteClient(FakeClient):
        def delete_objects(self, **kwargs):
            entered.set()
            assert release.wait(timeout=5)
            return super().delete_objects(**kwargs)

    client = BlockingDeleteClient()
    service = R2Service(make_config(), client)
    group = "uploads/task/"
    key = f"{group}file.bin"
    result = []

    worker = threading.Thread(target=lambda: result.append(service.delete_keys([key])))
    worker.start()
    assert entered.wait(timeout=5)
    with pytest.raises(RuntimeError, match="being deleted"):
        service.register_active_group(group)
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert result == [1]
    service.register_active_group(group)
    assert service.is_group_active(group)


def test_metadata_cache_reuses_identity_and_refreshes_on_change():
    client = FakeClient()
    key = "uploads/task/file.bin"
    client.metadata[key] = {"mirror-kind": "file"}
    service = R2Service(make_config(), client)
    first = listed(key)

    assert service.metadata_for(first) == {"mirror-kind": "file"}
    assert service.metadata_for(dict(first)) == {"mirror-kind": "file"}
    assert len(client.head_calls) == 1
    changed = listed(key, etag="two")
    service.metadata_for(changed)
    assert len(client.head_calls) == 2


def test_managed_client_close_is_idempotent():
    client = FakeClient()
    service = R2Service(make_config(), client)

    service.close()
    service.close()

    assert client.close_calls == 1
