import json
import logging
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)
INTERNAL_URL = "http://jellyfin:8096"
YEAR_SUFFIX = re.compile(r"\s+\(\d{4}\)$")


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

    def scan_library(self) -> None:
        self._request("POST", "/Library/Refresh")
        LOGGER.info("Jellyfin library scan requested")

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

    def refresh_new_media(
        self,
        name: str,
        media_type: str,
        attempts: int = 12,
        delay: float = 5,
    ) -> str:
        self.scan_library()
        item_type = "Series" if media_type == "series" else "Movie"
        search_name = YEAR_SUFFIX.sub("", name).strip()
        normalized_name = search_name.casefold()
        for attempt in range(attempts):
            if attempt:
                time.sleep(delay)
            result = self._request(
                "GET",
                "/Items",
                {
                    "recursive": "true",
                    "searchTerm": search_name,
                    "includeItemTypes": item_type,
                    "fields": "Path",
                    "limit": 20,
                },
            )
            items = result.get("Items", [])
            exact = [
                item
                for item in items
                if str(item.get("Name", "")).casefold().strip() == normalized_name
            ]
            match = exact[0] if exact else items[0] if len(items) == 1 else None
            if match and match.get("Id"):
                self.refresh_item_metadata(match["Id"])
                return match["Id"]
        raise RuntimeError(f"Jellyfin did not discover the new {media_type}: {name}")

    def refresh_all_metadata(self) -> int:
        self.scan_library()
        libraries = self._request("GET", "/Library/VirtualFolders")
        item_ids = [library.get("ItemId") for library in libraries if library.get("ItemId")]
        for item_id in item_ids:
            self.refresh_item_metadata(item_id)
        LOGGER.info(
            "Jellyfin metadata refresh requested for %s libraries",
            len(item_ids),
        )
        return len(item_ids)
