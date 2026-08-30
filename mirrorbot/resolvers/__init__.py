import json
import logging
from urllib.parse import urlparse

import aiohttp

from ..core.logging_config import log_event
from ..core.models import Source, SourceType
from .base import USER_AGENT, ResolvedCollection, ResolverError, resolved_source
from .direct_hosts import (
    KrakenFilesResolver,
    PCloudResolver,
    SendCmResolver,
    SolidFilesResolver,
    StreamTapeResolver,
    UploadEeResolver,
)
from .doodstream import DoodstreamResolver
from .fichier import FichierResolver
from .gofile import GoFileResolver
from .linkbox import LinkboxResolver
from .mediafire import MediaFireResolver
from .onedrive import OneDriveResolver
from .ouo import OuoResolver
from .pixeldrain import PixelDrainResolver
from .racaty import RacatyResolver
from .redirects import RedirectResolver
from .wetransfer import WeTransferResolver

LOGGER = logging.getLogger(__name__)
RESOLVERS = (
    RedirectResolver(),
    OuoResolver(),
    MediaFireResolver(),
    PixelDrainResolver(),
    WeTransferResolver(),
    OneDriveResolver(),
    GoFileResolver(),
    SolidFilesResolver(),
    UploadEeResolver(),
    StreamTapeResolver(),
    PCloudResolver(),
    SendCmResolver(),
    KrakenFilesResolver(),
    FichierResolver(),
    RacatyResolver(),
    DoodstreamResolver(),
    LinkboxResolver(),
)


def is_resolvable_url(url: str) -> bool:
    return any(resolver.supports(url) for resolver in RESOLVERS)


def _cause_summary(exc: Exception) -> str:
    """A short, non-sensitive reason for a user-facing resolver error."""
    if isinstance(exc, aiohttp.ClientResponseError):
        return f"host returned HTTP {exc.status}"
    if isinstance(exc, (aiohttp.ClientError, TimeoutError)):
        return "host could not be reached"
    if isinstance(exc, json.JSONDecodeError):
        return "host returned an unexpected response"
    if isinstance(exc, (KeyError, IndexError, ValueError, TypeError)):
        return "host returned data in an unexpected format"
    return type(exc).__name__


async def resolve_source(
    source: Source, session: aiohttp.ClientSession | None = None
) -> Source:
    if source.type != SourceType.DIRECT_URL:
        return source
    if session is not None:
        return await _resolve_with(source, session)
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as owned:
        return await _resolve_with(source, owned)


async def _resolve_with(source: Source, session: aiohttp.ClientSession) -> Source:
    current = source
    for _ in range(3):
        resolver = next(
            (candidate for candidate in RESOLVERS if candidate.supports(current.value)),
            None,
        )
        if resolver is None:
            return current
        original = current.value
        try:
            result = await resolver.resolve(original, session)
        except ResolverError:
            raise
        except Exception as exc:
            log_event(
                LOGGER,
                logging.WARNING,
                "resolver.failed",
                resolver=resolver.name,
                host=urlparse(original).hostname or "unknown-host",
                error_type=type(exc).__name__,
                error=exc,
            )
            raise ResolverError(
                f"{resolver.name} could not resolve this link ({_cause_summary(exc)})"
            ) from exc
        current = resolved_source(current, result, resolver.name)
        resolved_target = (
            f"collection:{len(result.files)}"
            if isinstance(result, ResolvedCollection)
            else urlparse(result.url).hostname or "unknown-host"
        )
        log_event(
            LOGGER,
            logging.INFO,
            "resolver.resolved",
            resolver=resolver.name,
            target=resolved_target,
        )
        if isinstance(result, ResolvedCollection):
            return current
        if resolver.name == "redirect":
            from ..core.source_detector import detect_source

            detected = detect_source(current.value, current.filename)
            if detected.type != SourceType.DIRECT_URL:
                detected.metadata.update(current.metadata)
                return detected
        if current.value == original:
            return current
    if any(resolver.supports(current.value) for resolver in RESOLVERS):
        raise ResolverError(
            "This link kept redirecting and never resolved to a downloadable file"
        )
    return current
