from urllib.parse import urlparse

from .base import ResolvedDownload, ResolverError, host_matches


def transfer_parts(url: str) -> tuple[str, str]:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if "downloads" in parts:
        index = parts.index("downloads")
        if len(parts) > index + 2:
            return parts[index + 1], parts[index + 2]
    raise ResolverError("Invalid WeTransfer link")


class WeTransferResolver:
    name = "wetransfer"
    domains = ("wetransfer.com", "we.tl")

    def supports(self, url: str) -> bool:
        return host_matches(url, self.domains)

    async def resolve(self, url, session) -> ResolvedDownload:
        async with session.get(url, allow_redirects=True) as response:
            response.raise_for_status()
            final_url = str(response.url)
        transfer_id, security_hash = transfer_parts(final_url)
        async with session.post(
            f"https://wetransfer.com/api/v4/transfers/{transfer_id}/download",
            json={"security_hash": security_hash, "intent": "entire_transfer"},
        ) as response:
            response.raise_for_status()
            data = await response.json()
        if not data.get("direct_link"):
            raise ResolverError(data.get("message") or "WeTransfer link was not found")
        return ResolvedDownload(data["direct_link"])
