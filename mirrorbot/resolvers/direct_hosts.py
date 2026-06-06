import html
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from .base import ResolvedDownload, ResolverError, host_matches


class SolidFilesResolver:
    name = "solidfiles"
    domains = ("solidfiles.com",)

    def supports(self, url: str) -> bool:
        return host_matches(url, self.domains)

    async def resolve(self, url, session) -> ResolvedDownload:
        async with session.get(url) as response:
            response.raise_for_status()
            page = await response.text()
        match = re.search(r"viewerOptions'\s*,\s*(\{.*?\})\s*\);", page)
        if not match:
            raise ResolverError("SolidFiles download data was not found")
        data = json.loads(match.group(1))
        return ResolvedDownload(data["downloadUrl"], filename=data.get("name", ""))


class UploadEeResolver:
    name = "upload.ee"
    domains = ("upload.ee",)

    def supports(self, url: str) -> bool:
        return host_matches(url, self.domains) and "/files/" in urlparse(url).path

    async def resolve(self, url, session) -> ResolvedDownload:
        async with session.get(url) as response:
            response.raise_for_status()
            page = await response.text()
        match = re.search(r'<a[^>]+href="([^"]+)"[^>]+id="d_l"', page)
        if not match:
            match = re.search(r'<a[^>]+id="d_l"[^>]+href="([^"]+)"', page)
        if not match:
            raise ResolverError("Upload.ee download link was not found")
        direct = html.unescape(match.group(1))
        return ResolvedDownload(direct, Path(urlparse(direct).path).name)


class StreamTapeResolver:
    name = "streamtape"
    domains = (
        "streamtape.com",
        "streamtape.co",
        "streamtape.cc",
        "streamtape.to",
        "streamtape.net",
        "streamtape.xyz",
    )

    def supports(self, url: str) -> bool:
        return host_matches(url, self.domains)

    async def resolve(self, url, session) -> ResolvedDownload:
        async with session.get(url) as response:
            response.raise_for_status()
            page = await response.text()
        parts = [part for part in urlparse(url).path.split("/") if part]
        file_id = parts[-2] if len(parts) > 1 and parts[-2] not in {"v", "e"} else parts[-1]
        matches = re.findall(r"(&expires[^'\"]+)", page)
        if not matches:
            raise ResolverError("StreamTape download link was not found")
        return ResolvedDownload(f"https://streamtape.com/get_video?id={file_id}{matches[-1]}")


class PCloudResolver:
    name = "pcloud"
    domains = ("pcloud.link",)

    def supports(self, url: str) -> bool:
        return host_matches(url, self.domains)

    async def resolve(self, url, session) -> ResolvedDownload:
        async with session.get(url) as response:
            response.raise_for_status()
            page = await response.text()
        match = re.search(r'["\']downloadlink["\']\s*:\s*["\'](https:.*?)[\'"]', page)
        if not match:
            raise ResolverError("pCloud download link was not found")
        return ResolvedDownload(match.group(1).replace("\\/", "/"))


class SendCmResolver:
    name = "send.cm"
    domains = ("send.cm",)

    def supports(self, url: str) -> bool:
        return host_matches(url, self.domains) and "/s/" not in urlparse(url).path

    async def resolve(self, url, session) -> ResolvedDownload:
        file_id = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        if "/d/" in urlparse(url).path:
            async with session.get(url) as response:
                response.raise_for_status()
                page = await response.text()
            match = re.search(r'<input[^>]+name=["\']id["\'][^>]+value=["\']([^"\']+)', page)
            if not match:
                match = re.search(r'<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']id["\']', page)
            if not match:
                raise ResolverError("Send.cm file ID was not found")
            file_id = match.group(1)
        async with session.post(
            "https://send.cm/",
            data={"op": "download2", "id": file_id},
            allow_redirects=False,
        ) as response:
            location = response.headers.get("Location", "")
        if not location:
            raise ResolverError("Send.cm download link was not found")
        return ResolvedDownload(location, headers={"Referer": "https://send.cm/"})


class KrakenFilesResolver:
    name = "krakenfiles"
    domains = ("krakenfiles.com",)

    def supports(self, url: str) -> bool:
        return host_matches(url, self.domains)

    async def resolve(self, url, session) -> ResolvedDownload:
        async with session.get(url) as response:
            response.raise_for_status()
            page = await response.text()
        action = re.search(r'<form[^>]+id=["\']dl-form["\'][^>]+action=["\']([^"\']+)', page)
        token = re.search(r'<input[^>]+id=["\']dl-token["\'][^>]+value=["\']([^"\']+)', page)
        if not action or not token:
            raise ResolverError("KrakenFiles download form was not found")
        post_url = action.group(1)
        if post_url.startswith("/"):
            post_url = f"https://krakenfiles.com{post_url}"
        async with session.post(post_url, data={"token": token.group(1)}) as response:
            response.raise_for_status()
            payload = await response.json()
        direct = payload.get("url")
        if not direct:
            raise ResolverError("KrakenFiles download link was not found")
        return ResolvedDownload(direct)
