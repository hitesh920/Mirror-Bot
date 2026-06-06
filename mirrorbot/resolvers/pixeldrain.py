from urllib.parse import urlparse

from .base import ResolvedDownload, ResolverError, host_matches


class PixelDrainResolver:
    name = "pixeldrain"
    domains = ("pixeldrain.com", "pixeldra.in")

    def supports(self, url: str) -> bool:
        parts = [part for part in urlparse(url).path.split("/") if part]
        return host_matches(url, self.domains) and bool(
            parts and parts[0] in {"u", "l"}
        )

    async def resolve(self, url, _session) -> ResolvedDownload:
        parts = [part for part in urlparse(url).path.split("/") if part]
        if len(parts) < 2 or parts[0] not in {"u", "l"}:
            raise ResolverError("Invalid PixelDrain link")
        kind, item_id = parts[0], parts[1]
        if kind == "l":
            return ResolvedDownload(
                f"https://pixeldrain.com/api/list/{item_id}/zip",
                filename=f"pixeldrain-{item_id}.zip",
            )
        return ResolvedDownload(f"https://pixeldrain.com/api/file/{item_id}?download")
