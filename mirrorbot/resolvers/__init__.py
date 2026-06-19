import logging

import aiohttp

from ..core.models import Source, SourceType
from .base import USER_AGENT, ResolvedCollection, ResolverError, resolved_source
from .buzzheavier import BuzzHeavierResolver
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
    BuzzHeavierResolver(),
)


def is_resolvable_url(url: str) -> bool:
    return any(resolver.supports(url) for resolver in RESOLVERS)


async def resolve_source(source: Source) -> Source:
    if source.type != SourceType.DIRECT_URL:
        return source

    current = source
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
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
                raise ResolverError(
                    f"{resolver.name} could not resolve this link"
                ) from exc
            current = resolved_source(current, result, resolver.name)
            resolved_target = (
                f"collection:{len(result.files)}"
                if isinstance(result, ResolvedCollection)
                else result.url
            )
            LOGGER.info(
                "Resolved direct-host link resolver=%s target=%s",
                resolver.name,
                resolved_target,
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
    return current
