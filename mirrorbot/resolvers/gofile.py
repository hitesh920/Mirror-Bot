from hashlib import sha256
from time import time
from urllib.parse import urlparse

from .base import (
    USER_AGENT,
    ResolvedCollection,
    ResolvedDownload,
    ResolvedFile,
    ResolverError,
    host_matches,
)


class GoFileResolver:
    name = "gofile"
    domains = ("gofile.io",)

    def supports(self, url: str) -> bool:
        return host_matches(url, self.domains) and "/d/" in urlparse(url).path

    async def resolve(self, url, session) -> ResolvedDownload | ResolvedCollection:
        content_id = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        if not content_id:
            raise ResolverError("Invalid Gofile link")

        async with session.post("https://api.gofile.io/accounts") as response:
            response.raise_for_status()
            account = await response.json()
        if account.get("status") != "ok":
            raise ResolverError("Gofile could not create a guest account")
        token = account["data"]["token"]
        headers = {
            "Authorization": f"Bearer {token}",
            "X-BL": "en-US",
            "X-Website-Token": sha256(
                f"{USER_AGENT}::en-US::{token}::{int(time()) // 14400}::gf2026x".encode()
            ).hexdigest(),
        }
        collection = ResolvedCollection(
            title=content_id,
            headers=headers,
            cookies={"accountToken": token},
        )
        await self._collect(content_id, "", collection, session, headers)
        if len(collection.files) == 1:
            item = collection.files[0]
            return ResolvedDownload(
                item.url,
                item.filename,
                headers=collection.headers,
                cookies=collection.cookies,
            )
        return collection

    async def _collect(self, content_id, path, collection, session, headers):
        async with session.get(
            f"https://api.gofile.io/contents/{content_id}?cache=true",
            headers=headers,
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        status = payload.get("status", "")
        errors = {
            "error-passwordRequired": "Gofile link requires a password",
            "error-passwordWrong": "Gofile password is incorrect",
            "error-notFound": "Gofile content was not found",
            "error-notPublic": "Gofile content is not public",
        }
        if status != "ok":
            raise ResolverError(errors.get(status, f"Gofile returned {status or 'an error'}"))
        data = payload["data"]
        if collection.title == content_id:
            collection.title = data.get("name") or content_id
        children = data.get("children") or {}
        if data.get("type") == "file":
            children = {data.get("id", content_id): data}
        for item in children.values():
            name = item.get("name") or item.get("id", "unnamed")
            if item.get("type") == "folder":
                if item.get("public", True):
                    await self._collect(item["id"], f"{path}/{name}".strip("/"), collection, session, headers)
                continue
            collection.files.append(
                ResolvedFile(
                    item["link"],
                    name,
                    path,
                    int(item.get("size") or 0),
                )
            )
