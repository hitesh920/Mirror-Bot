import json
import logging
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)
INTERNAL_URL = "http://jellyfin:8096"


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
