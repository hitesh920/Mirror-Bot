import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from mirrorbot.services import r2_delivery
from mirrorbot.services.r2_delivery import display_name_for_key

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC).timestamp()


def config(tmp_path=None):
    log_file = "logs/bot.log" if tmp_path is None else str(tmp_path / "bot.log")
    return SimpleNamespace(
        r2_configured=True,
        r2_bucket="mirror-bot",
        r2_prefix="uploads/",
        r2_auto_delete_seconds=86400,
        log_file=log_file,
    )


class FakeClient:
    def __init__(self, objects, metadata):
        self.objects = objects
        self.metadata = metadata
        self.calls = []
        self.closed = 0

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return self

    def paginate(self, **kwargs):
        self.calls.append(("list", kwargs["Prefix"]))
        yield {"Contents": self.objects}

    def head_object(self, **kwargs):
        key = kwargs["Key"]
        self.calls.append(("head", key))
        return {"Metadata": self.metadata.get(key, {})}

    def delete_objects(self, **kwargs):
        keys = [item["Key"] for item in kwargs["Delete"]["Objects"]]
        self.calls.append(("delete", tuple(keys)))
        return {"Deleted": [{"Key": key} for key in keys]}

    def close(self):
        self.closed += 1


def test_config_retention_uses_one_client_and_stored_standalone_expiry(
    tmp_path,
    monkeypatch,
):
    key = "uploads/task/movie.mkv"
    objects = [
        {
            "Key": key,
            "ETag": '"v1"',
            "Size": 10,
            "LastModified": datetime.fromtimestamp(NOW, UTC),
        }
    ]
    client = FakeClient(
        objects,
        {
            key: {
                "mirror-kind": "file",
                "expires-at": str(int(NOW) - 1),
            }
        },
    )
    monkeypatch.setattr(r2_delivery, "r2_client", lambda _config: client)

    assert r2_delivery.delete_expired_objects(config(tmp_path), now=NOW) == 1
    assert client.calls == [
        ("list", "uploads/"),
        ("head", key),
        ("delete", (key,)),
    ]
    assert client.closed == 1


def test_config_retention_recognizes_metadata_only_folder(monkeypatch):
    page_key = "uploads/task/index.html"
    file_key = "uploads/task/folder/movie.mkv"
    objects = [
        {
            "Key": page_key,
            "ETag": '"page-v1"',
            "Size": 3,
            "LastModified": datetime.fromtimestamp(NOW, UTC),
        },
        {
            "Key": file_key,
            "ETag": '"file-v1"',
            "Size": 20,
            "LastModified": datetime.fromtimestamp(NOW, UTC),
        },
    ]
    client = FakeClient(
        objects,
        {
            page_key: {
                "mirror-kind": "folder",
                "expires-at": str(int(NOW) + 60),
            },
            file_key: {
                "mirror-kind": "file",
                "expires-at": str(int(NOW) + 3600),
            },
        },
    )
    monkeypatch.setattr(r2_delivery, "r2_client", lambda _config: client)

    warnings = r2_delivery.expiring_uploads(config(), now=NOW)

    assert len(warnings) == 1
    assert warnings[0]["kind"] == "folder"
    assert warnings[0]["key"] == page_key
    assert warnings[0]["keys"] == (page_key, file_key)
    assert warnings[0]["objects"] == 1
    assert warnings[0]["bytes"] == 20
    assert client.closed == 1


@pytest.mark.asyncio
async def test_expiry_sweeper_accepts_config_and_closes_on_cancel(
    tmp_path,
    monkeypatch,
):
    client = FakeClient([], {})
    started = asyncio.Event()

    async def fake_sweeper(service, notify_warning):
        assert service.config.r2_bucket == "mirror-bot"
        assert notify_warning == "callback"
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(r2_delivery, "r2_client", lambda _config: client)
    monkeypatch.setattr(r2_delivery, "_expiry_sweeper", fake_sweeper)

    task = asyncio.create_task(r2_delivery.expiry_sweeper(config(tmp_path), "callback"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.closed == 1


def test_display_name_for_key_remains_public():
    assert (
        display_name_for_key("uploads/task/Season 1.mirrorbot-folder.html")
        == "Season 1"
    )
    assert "display_name_for_key" in r2_delivery.__all__
