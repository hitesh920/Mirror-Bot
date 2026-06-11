import json
import logging
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)
INTERNAL_URL = "http://jellyfin:8096"


class JellyfinApi:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _request(self, method: str, path: str):
        if not self.api_key:
            raise RuntimeError("JELLYFIN_API_KEY is not configured")
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
