import html
import re
from urllib.parse import urljoin, urlparse

import aiohttp

from .base import (
    USER_AGENT,
    ResolvedCollection,
    ResolvedDownload,
    ResolvedFile,
    ResolverError,
    host_matches,
    safe_name,
)


SIZE_UNITS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


def _strip_tags(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", value)).strip()


def _tag_attr(tag: str, name: str) -> str:
    match = re.search(rf"""{name}\s*=\s*["']([^"']+)["']""", tag, re.I)
    return html.unescape(match.group(1)) if match else ""


def _parse_size(value: str) -> int:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B?)", value, re.I)
    if not match:
        return 0
    unit = (match.group(2) or "b").lower()
    if unit in {"k", "m", "g", "t"}:
        unit += "b"
    return int(float(match.group(1)) * SIZE_UNITS.get(unit, 1))


class BuzzHeavierResolver:
    name = "buzzheavier"

    def supports(self, url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.scheme in {"http", "https"}
            and host_matches(url, ("buzzheavier.com",))
            and bool(parsed.path.strip("/").split("/", 1)[0])
        )

    async def resolve(
        self, url: str, session: aiohttp.ClientSession
    ) -> ResolvedDownload | ResolvedCollection:
        async with session.get(url, allow_redirects=True) as response:
            if response.status >= 400:
                raise ResolverError("BuzzHeavier link is unavailable")
            page_url = str(response.url)
            text = await response.text()

        collection = await self._resolve_collection(page_url, text, session)
        if collection.files:
            return collection

        filename = self._page_title(text) or safe_name(urlparse(page_url).path, "buzzheavier")
        direct_url = await self._direct_url(page_url, text, session)
        return ResolvedDownload(direct_url, filename)

    async def _resolve_collection(
        self, page_url: str, text: str, session: aiohttp.ClientSession
    ) -> ResolvedCollection:
        files: list[ResolvedFile] = []
        tbody = re.search(
            r"""<tbody[^>]+id=["']tbody["'][^>]*>(.*?)</tbody>""",
            text,
            re.I | re.S,
        )
        if not tbody:
            return ResolvedCollection(self._page_title(text) or "BuzzHeavier")

        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", tbody.group(1), re.I | re.S):
            anchor = re.search(r"<a\b[^>]*href=[\"'][^\"']+[\"'][^>]*>.*?</a>", row, re.I | re.S)
            if not anchor:
                continue
            href = _tag_attr(anchor.group(0), "href")
            filename = safe_name(_strip_tags(anchor.group(0)))
            if not href or not filename:
                continue
            try:
                direct_url = await self._direct_url(urljoin(page_url, href), row, session)
            except ResolverError:
                continue
            cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.I | re.S)
            size = max((_parse_size(_strip_tags(cell)) for cell in cells), default=0)
            files.append(ResolvedFile(direct_url, filename, size=size))

        return ResolvedCollection(self._page_title(text) or "BuzzHeavier", files)

    async def _direct_url(
        self, page_url: str, text: str, session: aiohttp.ClientSession
    ) -> str:
        hx_get = ""
        for tag in re.findall(r"<a\b[^>]*>", text, re.I | re.S):
            classes = _tag_attr(tag, "class")
            if "link-button" in classes and "gay-button" in classes:
                hx_get = _tag_attr(tag, "hx-get")
                if hx_get:
                    break
        target = urljoin(page_url, hx_get) if hx_get else page_url
        if not target.rstrip("/").endswith("/download"):
            target = f"{target.rstrip('/')}/download"
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": page_url,
            "HX-Current-URL": page_url,
            "HX-Request": "true",
            "Priority": "u=1, i",
        }
        async with session.get(target, headers=headers, allow_redirects=False) as response:
            if response.status >= 400:
                raise ResolverError("BuzzHeavier could not create a download link")
            direct_url = response.headers.get("Hx-Redirect") or response.headers.get("HX-Redirect")
        if not direct_url:
            raise ResolverError("BuzzHeavier did not return a direct download link")
        return direct_url

    @staticmethod
    def _page_title(text: str) -> str:
        match = re.search(r"<title\b[^>]*>(.*?)</title>", text, re.I | re.S)
        if match:
            title = _strip_tags(match.group(1))
            title = re.sub(r"\s*[-|]\s*BuzzHeavier\s*$", "", title, flags=re.I).strip()
            if title:
                return safe_name(title)
        span = re.search(r"<span\b[^>]*>(.*?)</span>", text, re.I | re.S)
        return safe_name(_strip_tags(span.group(1)), "BuzzHeavier") if span else "BuzzHeavier"
