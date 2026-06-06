import asyncio
import re
from urllib.parse import urlparse

from .base import ResolvedDownload, ResolverError, host_matches

CSRF_PATTERN = re.compile(
    r'<input[^>]*\bname="_token"[^>]*\bvalue="([^"]*)"'
    r'|<input[^>]*\bvalue="([^"]*)"[^>]*\bname="_token"'
)


def extract_csrf(page: str) -> str:
    match = CSRF_PATTERN.search(page)
    return next((value for value in match.groups() if value), "") if match else ""


class OuoResolver:
    name = "ouo"
    domains = ("ouo.io", "ouo.press")

    def supports(self, url: str) -> bool:
        return host_matches(url, self.domains)

    async def resolve(self, url, _session) -> ResolvedDownload:
        return ResolvedDownload(await asyncio.to_thread(self._resolve_sync, url))

    def _resolve_sync(self, url: str) -> str:
        from curl_cffi import requests

        normalized = url.replace("ouo.press", "ouo.io")
        parsed = urlparse(normalized)
        short_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if not short_id:
            raise ResolverError("OUO link has no ID")
        base = f"{parsed.scheme}://{parsed.netloc}"
        go_url = f"{base}/go/{short_id}"
        final_url = f"{base}/xreallcygo/{short_id}"
        try:
            with requests.Session(impersonate="chrome136", timeout=30) as session:
                landing = session.get(normalized, allow_redirects=True)
                token = extract_csrf(landing.text)
                if not token:
                    raise ResolverError("OUO token was not found on the landing page")
                step = session.post(
                    go_url,
                    data={"_token": token, "x-token": "", "v-token": "vm"},
                    headers={"Origin": "https://ouo.io", "Referer": normalized},
                    allow_redirects=False,
                )
                token = extract_csrf(step.text)
                if not token:
                    raise ResolverError("OUO token was not found on the redirect page")
                result = session.post(
                    final_url,
                    data={"_token": token, "x-token": ""},
                    headers={"Origin": "https://ouo.io", "Referer": go_url},
                    allow_redirects=False,
                )
        except ResolverError:
            raise
        except Exception as exc:
            raise ResolverError(f"OUO bypass failed: {exc}") from exc
        location = result.headers.get("Location", "")
        if result.status_code != 302 or not location:
            raise ResolverError(f"OUO bypass returned HTTP {result.status_code}")
        return location
