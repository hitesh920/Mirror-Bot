import asyncio
import logging
from pathlib import Path
from typing import Any

import aiohttp

LOGGER = logging.getLogger(__name__)


class QBittorrentClient:
    def __init__(
        self,
        host: str,
        password_file: Path = Path("/app/data/qbittorrent/webui-password"),
    ):
        self.host = host.rstrip("/")
        self.password_file = password_file
        self.session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        return self.session

    async def login(self) -> None:
        session = await self._ensure_session()
        for _ in range(30):
            password = self._temporary_password()
            if password:
                async with session.post(
                    f"{self.host}/api/v2/auth/login",
                    data={"username": "admin", "password": password},
                ) as response:
                    body = await response.text()
                    if response.ok and body.strip() == "Ok.":
                        return
            await asyncio.sleep(1)
        raise RuntimeError("Could not authenticate with qBittorrent")

    def _temporary_password(self) -> str:
        if not self.password_file.exists():
            return ""
        return self.password_file.read_text(encoding="utf-8", errors="replace").strip()

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        data=None,
        params=None,
        files=None,
        retry_auth: bool = True,
    ) -> Any:
        session = await self._ensure_session()
        url = f"{self.host}/api/v2/{endpoint}"
        kwargs: dict[str, Any] = {"data": data, "params": params}
        if files:
            form = aiohttp.FormData()
            for key, value in (data or {}).items():
                form.add_field(key, str(value))
            for key, (filename, content) in files.items():
                form.add_field(
                    key,
                    content,
                    filename=filename,
                    content_type="application/x-bittorrent",
                )
            kwargs["data"] = form

        async with session.request(method, url, **kwargs) as response:
            if response.status == 403:
                if not retry_auth:
                    raise RuntimeError("qBittorrent authentication failed")
                await self.login()
                return await self.request(
                    method,
                    endpoint,
                    data=data,
                    params=params,
                    files=files,
                    retry_auth=False,
                )
            text = await response.text()
            if not response.ok:
                raise RuntimeError(
                    f"qBittorrent API {endpoint} failed ({response.status}): {text[:200]}"
                )
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                return await response.json()
            return text

    async def add(self, source: str | Path, save_path: Path, tag: str) -> None:
        data = {
            "savepath": str(save_path),
            "tags": tag,
            "stopped": str(isinstance(source, Path)).lower(),
            "stopCondition": (
                "None" if isinstance(source, Path) else "MetadataReceived"
            ),
        }
        if isinstance(source, Path):
            await self.request(
                "POST",
                "torrents/add",
                data=data,
                files={"torrents": (source.name, source.read_bytes())},
            )
        else:
            data["urls"] = source
            await self.request("POST", "torrents/add", data=data)

    async def info(self, *, tag: str = "", torrent_hash: str = "") -> list[dict]:
        params = {}
        if tag:
            params["tag"] = tag
        if torrent_hash:
            params["hashes"] = torrent_hash
        return await self.request("GET", "torrents/info", params=params)

    async def files(self, torrent_hash: str) -> list[dict]:
        return await self.request(
            "GET", "torrents/files", params={"hash": torrent_hash}
        )

    async def stop(self, torrent_hash: str) -> None:
        await self.request("POST", "torrents/stop", data={"hashes": torrent_hash})

    async def start(self, torrent_hash: str) -> None:
        await self.request("POST", "torrents/start", data={"hashes": torrent_hash})

    async def set_file_priority(
        self, torrent_hash: str, file_ids: list[int], priority: int
    ) -> None:
        if not file_ids:
            return
        await self.request(
            "POST",
            "torrents/filePrio",
            data={
                "hash": torrent_hash,
                "id": "|".join(str(file_id) for file_id in file_ids),
                "priority": str(priority),
            },
        )

    async def delete(self, torrent_hash: str, delete_files: bool) -> None:
        await self.request(
            "POST",
            "torrents/delete",
            data={
                "hashes": torrent_hash,
                "deleteFiles": str(delete_files).lower(),
            },
        )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
