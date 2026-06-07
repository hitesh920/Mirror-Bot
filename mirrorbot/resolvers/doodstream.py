import asyncio
import html
import re
from urllib.parse import urljoin, urlparse

from .base import ResolvedDownload, ResolverError, host_matches


DOOD_DOMAINS = (
    "dood.watch",
    "doodstream.com",
    "doodstream.co",
    "dood.to",
    "dood.so",
    "dood.cx",
    "dood.la",
    "dood.ws",
    "dood.sh",
    "dood.pm",
    "dood.wf",
    "dood.re",
    "dood.video",
    "dood.yt",
    "doods.yt",
    "dood.stream",
    "doods.pro",
)


def dood_token_link(page: str) -> str:
    match = re.search(
        r'<div[^>]+class=["\'][^"\']*download-content[^"\']*["\'][^>]*>.*?<a[^>]+href=["\']([^"\']+)',
        page,
        re.IGNORECASE | re.DOTALL,
    )
    return html.unescape(match.group(1)) if match else ""


def dood_download_link(page: str) -> str:
    match = re.search(r"window\.open\(['\"]([^'\"]+)", page)
    return html.unescape(match.group(1)) if match else ""


class DoodstreamResolver:
    name = "doodstream"
    domains = DOOD_DOMAINS

    def supports(self, url: str) -> bool:
        return host_matches(url, self.domains)

    async def resolve(self, url, session) -> ResolvedDownload:
        if "/e/" in url:
            url = url.replace("/e/", "/d/", 1)
        parsed = urlparse(url)
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        async with session.get(url) as response:
            response.raise_for_status()
            token = dood_token_link(await response.text())
        if not token:
            raise ResolverError("Doodstream token link was not found")
        await asyncio.sleep(2)
        async with session.get(urljoin(referer, token)) as response:
            response.raise_for_status()
            direct = dood_download_link(await response.text())
        if not direct:
            raise ResolverError("Doodstream download link was not found")
        return ResolvedDownload(direct, headers={"Referer": referer})
