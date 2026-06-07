import html
import re
from urllib.parse import urlparse

from .base import ResolvedDownload, ResolverError, host_matches


def racaty_download_link(page: str) -> str:
    match = re.search(
        r'<a[^>]+id=["\']uniqueExpirylink["\'][^>]+href=["\']([^"\']+)',
        page,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]+id=["\']uniqueExpirylink["\']',
            page,
            re.IGNORECASE,
        )
    return html.unescape(match.group(1)) if match else ""


class RacatyResolver:
    name = "racaty"
    domains = ("racaty.io", "racaty.net")

    def supports(self, url: str) -> bool:
        return host_matches(url, self.domains)

    async def resolve(self, url, session) -> ResolvedDownload:
        async with session.get(url) as response:
            response.raise_for_status()
            final_url = str(response.url)
        file_id = urlparse(final_url).path.rstrip("/").rsplit("/", 1)[-1]
        async with session.post(
            final_url, data={"op": "download2", "id": file_id}
        ) as response:
            response.raise_for_status()
            page = await response.text()
        direct = racaty_download_link(page)
        if not direct:
            raise ResolverError("Racaty download link was not found")
        return ResolvedDownload(direct)
