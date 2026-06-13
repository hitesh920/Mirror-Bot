import pytest

from mirrorbot.services.drive_sharing import DriveShareError, build_drive_share
from mirrorbot.services.google_drive_delivery import FOLDER_MIME_TYPE


class Request:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class Files:
    def __init__(self, items, children):
        self.items = items
        self.children = children

    def get(self, fileId, **_):
        return Request(self.items[fileId])

    def list(self, q, **_):
        parent_id = q.split("'")[1]
        return Request({"files": self.children.get(parent_id, [])})


class Permissions:
    def __init__(self, public_ids):
        self.public_ids = public_ids

    def list(self, fileId, **_):
        permissions = [{"type": "anyone", "role": "reader"}] if fileId in self.public_ids else []
        return Request({"permissions": permissions})


class Drive:
    def __init__(self, items, children, public_ids):
        self._files = Files(items, children)
        self._permissions = Permissions(public_ids)

    def files(self):
        return self._files

    def permissions(self):
        return self._permissions


def test_public_nested_folder_native_file_and_shortcut(monkeypatch, config):
    items = {
        "root": {"id": "root", "name": "Public", "mimeType": FOLDER_MIME_TYPE},
        "sub": {"id": "sub", "name": "Sub", "mimeType": FOLDER_MIME_TYPE},
        "binary": {
            "id": "binary",
            "name": "Movie.mkv",
            "mimeType": "video/x-matroska",
            "resourceKey": "public-key",
        },
        "doc": {
            "id": "doc",
            "name": "Notes",
            "mimeType": "application/vnd.google-apps.document",
            "webViewLink": "https://docs.google.com/document/d/doc/edit",
        },
    }
    shortcut = {
        "id": "shortcut",
        "name": "Movie shortcut.mkv",
        "mimeType": "application/vnd.google-apps.shortcut",
        "shortcutDetails": {"targetId": "binary", "targetMimeType": "video/x-matroska"},
    }
    children = {"root": [items["sub"], items["doc"]], "sub": [shortcut]}
    drive = Drive(items, children, set(items))
    monkeypatch.setattr("mirrorbot.services.drive_sharing.drive_service", lambda _: drive)

    manifest = build_drive_share(config, "root")

    assert manifest.name == "Public"
    assert manifest.folder_count == 2
    assert [(item.name, item.folder_path) for item in manifest.files] == [
        ("Movie shortcut.mkv", "Public/Sub"),
        ("Notes", "Public"),
    ]
    assert manifest.files[0].url == (
        "https://drive.google.com/uc?id=binary&export=download&resourcekey=public-key"
    )
    assert manifest.files[1].url == items["doc"]["webViewLink"]


def test_private_child_rejects_complete_share(monkeypatch, config):
    items = {
        "root": {"id": "root", "name": "Public", "mimeType": FOLDER_MIME_TYPE},
        "private": {"id": "private", "name": "Private.bin", "mimeType": "application/octet-stream"},
    }
    drive = Drive(items, {"root": [items["private"]]}, {"root"})
    monkeypatch.setattr("mirrorbot.services.drive_sharing.drive_service", lambda _: drive)

    with pytest.raises(DriveShareError, match="Private.bin"):
        build_drive_share(config, "root")


def test_empty_folder_is_rejected(monkeypatch, config):
    root = {"id": "root", "name": "Empty", "mimeType": FOLDER_MIME_TYPE}
    drive = Drive({"root": root}, {"root": []}, {"root"})
    monkeypatch.setattr("mirrorbot.services.drive_sharing.drive_service", lambda _: drive)

    with pytest.raises(DriveShareError, match="empty"):
        build_drive_share(config, "root")


def test_single_public_file_is_shared(monkeypatch, config):
    item = {
        "id": "file",
        "name": "Movie.mkv",
        "mimeType": "video/x-matroska",
        "resourceKey": "key",
    }
    drive = Drive({"file": item}, {}, {"file"})
    monkeypatch.setattr("mirrorbot.services.drive_sharing.drive_service", lambda _: drive)

    manifest = build_drive_share(config, "file")

    assert manifest.name == "Movie.mkv"
    assert manifest.is_folder is False
    assert manifest.folder_count == 0
    assert manifest.files[0].name == "Movie.mkv"
    assert manifest.files[0].url.endswith("&resourcekey=key")
