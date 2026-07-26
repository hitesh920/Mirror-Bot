from urllib.parse import unquote, urlparse

from .base import ResolvedDownload, ResolverError, host_matches


class RedirectResolver:
    name = "redirect"
    domains = (
        "bit.ly",
        "tinyurl.com",
        "t.co",
        "is.gd",
        "cutt.ly",
        "shorturl.at",
        "rb.gy",
        "rebrand.ly",
        "href.li",
    )

    def supports(self, url: str) -> bool:
        return host_matches(url, self.domains)

    async def resolve(self, url, session) -> ResolvedDownload:
        if host_matches(url, ("href.li",)):
            target = unquote(urlparse(url).query)
            if urlparse(target).scheme not in {"http", "https"}:
                raise ResolverError("href.li target URL is invalid")
            return ResolvedDownload(target)

        async with session.get(url, allow_redirects=True) as response:
            response.raise_for_status()
            final_url = str(response.url)
        if final_url == url:
            raise ResolverError("Shortened URL did not redirect")
        return ResolvedDownload(final_url)
