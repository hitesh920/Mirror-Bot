import html
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from .base import ResolvedCollection, ResolvedDownload, ResolvedFile, ResolverError, host_matches

DOWNLOAD_PATTERNS = (
    re.compile(r'<a[^>]+aria-label=["\']Download file["\'][^>]+href=["\']([^"\']+)'),
    re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]+aria-label=["\']Download file["\']'),
    re.compile(r'<a[^>]+id=["\']downloadButton["\'][^>]+href=["\']([^"\']+)'),
)


def extract_mediafire_link(page: str) -> str:
    for pattern in DOWNLOAD_PATTERNS:
        if match := pattern.search(page):
            return html.unescape(match.group(1))
    return ""


class MediaFireResolver:
    name = "mediafire"
    domains = ("mediafire.com",)

    def supports(self, url: str) -> bool:
        return host_matches(url, self.domains) and "download" not in (
            urlparse(url).hostname or ""
        )

    async def resolve(self, url, session) -> ResolvedDownload | ResolvedCollection:
        if "/folder/" in url:
            return await self._resolve_folder(url, session)
        async with session.get(url, allow_redirects=True) as response:
            response.raise_for_status()
            page = await response.text()
        direct = extract_mediafire_link(page)
        if not direct:
            raise ResolverError("MediaFire download link was not found")
        filename = Path(unquote(urlparse(direct).path)).name
        return ResolvedDownload(direct, filename=filename)

    async def _resolve_folder(self, url, session) -> ResolvedCollection:
        raw = url.split("/folder/", 1)[1].split("/", 1)[0]
        keys = [key for key in raw.split(",") if key]
        if not keys:
            raise ResolverError("Invalid MediaFire folder link")
        info = await self._api(
            session,
            "folder/get_info.php",
            {"recursive": "yes", "folder_key": ",".join(keys)},
        )
        folder_infos = info.get("folder_infos") or [info.get("folder_info")]
        folder_infos = [item for item in folder_infos if item]
        if not folder_infos:
            raise ResolverError("MediaFire folder information was not found")
        collection = ResolvedCollection(folder_infos[0].get("name") or keys[0])
        multiple_roots = len(folder_infos) > 1
        for folder in folder_infos:
            await self._collect_folder(
                folder["folderkey"],
                folder.get("name", "") if multiple_roots else "",
                collection,
                session,
            )
        return collection

    async def _collect_folder(self, key, path, collection, session):
        folders = await self._api(
            session,
            "folder/get_content.php",
            {"content_type": "folders", "folder_key": key},
        )
        content = folders.get("folder_content") or {}
        for folder in content.get("folders") or []:
            child_path = f"{path}/{folder['name']}".strip("/")
            await self._collect_folder(folder["folderkey"], child_path, collection, session)

        files = await self._api(
            session,
            "folder/get_content.php",
            {"content_type": "files", "folder_key": key},
        )
        for item in (files.get("folder_content") or {}).get("files") or []:
            normal_url = (item.get("links") or {}).get("normal_download")
            if not normal_url:
                continue
            async with session.get(normal_url, allow_redirects=True) as response:
                response.raise_for_status()
                direct = extract_mediafire_link(await response.text())
            if direct:
                collection.files.append(
                    ResolvedFile(
                        direct,
                        item.get("filename") or Path(urlparse(direct).path).name,
                        path,
                        int(item.get("size") or 0),
                    )
                )

    async def _api(self, session, endpoint, data):
        async with session.post(
            f"https://www.mediafire.com/api/1.5/{endpoint}",
            data={**data, "response_format": "json"},
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        result = payload.get("response") or {}
        if result.get("message"):
            raise ResolverError(f"MediaFire: {result['message']}")
        return result
