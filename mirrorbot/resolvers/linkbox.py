from urllib.parse import urlparse

from .base import (
    ResolvedCollection,
    ResolvedDownload,
    ResolvedFile,
    ResolverError,
    host_matches,
)


def linkbox_filename(item: dict) -> str:
    name = item.get("name") or "unnamed"
    extension = str(item.get("sub_type") or "").lstrip(".")
    if extension and not name.lower().endswith(f".{extension.lower()}"):
        name = f"{name}.{extension}"
    return name


class LinkboxResolver:
    name = "linkbox"
    domains = ("linkbox.to", "linkbox.cloud")

    def supports(self, url: str) -> bool:
        return host_matches(url, self.domains)

    async def resolve(self, url, session) -> ResolvedDownload | ResolvedCollection:
        token = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        if not token:
            raise ResolverError("Invalid Linkbox link")
        payload = await self._request(
            session,
            "https://www.linkbox.to/api/file/share_out_list",
            {"shareToken": token, "pageSize": 1000, "pid": 0},
        )
        if payload.get("shareType") == "singleItem":
            return await self._single(session, payload.get("itemId"))
        collection = ResolvedCollection(payload.get("dirName") or token)
        await self._collect(token, payload, "", collection, session)
        if not collection.files:
            raise ResolverError("Linkbox folder is empty")
        return collection

    async def _single(self, session, item_id) -> ResolvedDownload:
        payload = await self._request(
            session,
            "https://www.linkbox.to/api/file/detail",
            {"itemId": item_id},
        )
        item = payload.get("itemInfo") or {}
        direct = item.get("url")
        if not direct:
            raise ResolverError("Linkbox file URL was not found")
        return ResolvedDownload(direct, linkbox_filename(item))

    async def _collect(self, token, payload, path, collection, session) -> None:
        for item in payload.get("list") or []:
            name = item.get("name") or str(item.get("id") or "unnamed")
            if item.get("type") == "dir" and not item.get("url"):
                child = await self._request(
                    session,
                    "https://www.linkbox.to/api/file/share_out_list",
                    {
                        "shareToken": token,
                        "pageSize": 1000,
                        "pid": item.get("id"),
                    },
                )
                await self._collect(
                    token,
                    child,
                    f"{path}/{name}".strip("/"),
                    collection,
                    session,
                )
                continue
            direct = item.get("url")
            if direct:
                collection.files.append(
                    ResolvedFile(
                        direct,
                        linkbox_filename(item),
                        path,
                        int(float(item.get("size") or 0)),
                    )
                )

    async def _request(self, session, url, params) -> dict:
        async with session.get(url, params=params) as response:
            response.raise_for_status()
            payload = await response.json()
        data = payload.get("data")
        if not data:
            raise ResolverError(payload.get("msg") or "Linkbox returned no data")
        return data
