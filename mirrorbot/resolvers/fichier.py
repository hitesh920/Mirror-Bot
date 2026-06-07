import html
import re

from .base import ResolvedDownload, ResolverError, host_matches


def fichier_download_link(page: str) -> str:
    match = re.search(
        r'<a[^>]+class=["\'][^"\']*\bok\b[^"\']*\bbtn-orange\b[^"\']*["\'][^>]+href=["\']([^"\']+)',
        page,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]+class=["\'][^"\']*\bok\b[^"\']*\bbtn-orange\b',
            page,
            re.IGNORECASE,
        )
    return html.unescape(match.group(1)) if match else ""


class FichierResolver:
    name = "1fichier"
    domains = ("1fichier.com",)

    def supports(self, url: str) -> bool:
        return host_matches(url, self.domains)

    async def resolve(self, url, session) -> ResolvedDownload:
        password = ""
        if "::" in url:
            url, password = url.rsplit("::", 1)
        async with session.post(url, data={"pass": password} if password else None) as response:
            if response.status == 404:
                raise ResolverError("1fichier file was not found")
            response.raise_for_status()
            page = await response.text()
        direct = fichier_download_link(page)
        if direct:
            return ResolvedDownload(direct)
        plain = re.sub(r"<[^>]+>", " ", page).lower()
        if "bad password" in plain:
            raise ResolverError("1fichier password is incorrect")
        if "protect access" in plain:
            raise ResolverError("1fichier link requires a password")
        if "you must wait" in plain:
            raise ResolverError("1fichier rate limit reached; try again later")
        raise ResolverError("1fichier download link was not found")
