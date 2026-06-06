from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Protocol
from urllib.parse import urlparse

import aiohttp

from ..models import Source, SourceType

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
)


class ResolverError(RuntimeError):
    pass


@dataclass
class ResolvedDownload:
    url: str
    filename: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)


@dataclass
class ResolvedFile:
    url: str
    filename: str
    path: str = ""
    size: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)


@dataclass
class ResolvedCollection:
    title: str
    files: list[ResolvedFile] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)

    @property
    def total_size(self) -> int:
        if any(item.size <= 0 for item in self.files):
            return 0
        return sum(item.size for item in self.files)


ResolvedResult = ResolvedDownload | ResolvedCollection


class Resolver(Protocol):
    name: str

    def supports(self, url: str) -> bool: ...

    async def resolve(
        self, url: str, session: aiohttp.ClientSession
    ) -> ResolvedResult: ...


def host_matches(url: str, domains: tuple[str, ...]) -> bool:
    host = urlparse(url).hostname or ""
    host = host.lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def safe_relative_path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ResolverError("Resolved collection contains an unsafe path")
    return str(path) if str(path) != "." else ""


def safe_name(value: str, fallback: str = "") -> str:
    name = PurePosixPath(value.replace("\\", "/")).name
    return fallback if name in {"", ".", ".."} else name


def resolved_source(original: Source, result: ResolvedResult, resolver_name: str) -> Source:
    metadata = dict(original.metadata)
    headers = {**(metadata.get("headers") or {}), **result.headers}
    cookies = {**(metadata.get("cookies") or {}), **result.cookies}
    metadata.update(
        {
            "original_url": metadata.get("original_url", original.value),
            "resolver": resolver_name,
            "headers": headers,
            "cookies": cookies,
        }
    )
    if isinstance(result, ResolvedCollection):
        if not result.files:
            raise ResolverError(f"{resolver_name} collection is empty")
        result.title = safe_name(result.title, "collection")
        for item in result.files:
            item.path = safe_relative_path(item.path)
            item.filename = safe_name(item.filename)
            if not item.filename:
                raise ResolverError(f"{resolver_name} returned an unnamed file")
        metadata["collection"] = result
        return Source(SourceType.DIRECT_URL, original.value, result.title, metadata)
    return Source(
        SourceType.DIRECT_URL,
        result.url,
        safe_name(result.filename or original.filename),
        metadata,
    )
