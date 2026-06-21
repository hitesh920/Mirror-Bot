import json
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)
INTERNAL_URL = "http://jellyfin:8096"
JELLYFIN_DATA_DIR = Path("/jellyfin-data")
MEDIA_ROOTS = {"/media", "/media/movies", "/media/series"}


class JellyfinApi:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _request(self, method: str, path: str, params: dict | None = None):
        if not self.api_key:
            raise RuntimeError("JELLYFIN_API_KEY is not configured")
        if params:
            path = f"{path}?{urlencode(params)}"
        request = Request(
            f"{INTERNAL_URL}{path}",
            method=method,
            headers={"X-Emby-Token": self.api_key, "Accept": "application/json"},
        )
        with urlopen(request, timeout=15) as response:
            body = response.read()
        return json.loads(body) if body else {}

    def system_info(self) -> dict:
        return self._request("GET", "/System/Info")

    def scan_library(self) -> int:
        self._request("POST", "/Library/Refresh")
        libraries = self._request("GET", "/Library/VirtualFolders")
        item_ids = [library.get("ItemId") for library in libraries if library.get("ItemId")]
        for item_id in item_ids:
            self.refresh_item_metadata(item_id)
        LOGGER.info(
            "Jellyfin library scan and metadata refresh requested for %s libraries",
            len(item_ids),
        )
        return len(item_ids)

    def _missing_media_rows(self, connection) -> list[tuple[str, str, str, str]]:
        rows = list(
            connection.execute(
                "select Id, Name, Path, Type from BaseItems where Path like ?",
                ("/media/%",),
            )
        )
        return [
            (item_id, name, item_path, item_type)
            for item_id, name, item_path, item_type in rows
            if item_path
            and item_path not in MEDIA_ROOTS
            and item_path.startswith("/media/")
            and not os.path.exists(item_path)
        ]

    def count_missing_media_items(self, data_dir: Path = JELLYFIN_DATA_DIR) -> int:
        db_path = data_dir / "jellyfin.db"
        if not db_path.exists():
            LOGGER.warning("Jellyfin missing-media count skipped: database is not mounted path=%s", db_path)
            return 0
        with sqlite3.connect(db_path) as connection:
            return len(self._missing_media_rows(connection))

    def prune_missing_media_items(self, data_dir: Path = JELLYFIN_DATA_DIR) -> int:
        db_path = data_dir / "jellyfin.db"
        if not db_path.exists():
            LOGGER.warning("Jellyfin missing-media prune skipped: database is not mounted path=%s", db_path)
            return 0

        with sqlite3.connect(db_path) as connection:
            stale_rows = self._missing_media_rows(connection)
            if not stale_rows:
                LOGGER.info("Jellyfin missing-media prune complete count=0")
                return 0

            backup_dir = data_dir / "SQLiteBackups" / f"mirrorbot-prune-{time.strftime('%Y%m%d-%H%M%S')}"
            backup_dir.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                source = Path(f"{db_path}{suffix}")
                if source.exists():
                    shutil.copy2(source, backup_dir / source.name)

            for item_id, _name, _item_path, _item_type in stale_rows:
                connection.execute("delete from BaseItems where Id = ?", (item_id,))
            connection.commit()

        removed = len(stale_rows)
        LOGGER.warning(
            "Jellyfin pruned missing media items count=%s backup=%s",
            removed,
            backup_dir,
        )
        return removed

    def refresh_item_metadata(self, item_id: str) -> None:
        self._request(
            "POST",
            f"/Items/{item_id}/Refresh",
            {
                "metadataRefreshMode": "FullRefresh",
                "imageRefreshMode": "FullRefresh",
                "replaceAllMetadata": "true",
                "replaceAllImages": "true",
                "regenerateTrickplay": "false",
            },
        )
        LOGGER.info("Jellyfin metadata refresh requested for item %s", item_id)
