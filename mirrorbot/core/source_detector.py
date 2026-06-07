from urllib.parse import urlparse

from yt_dlp import YoutubeDL
from yt_dlp.extractor import gen_extractors

from .models import Source, SourceType
from ..resolvers import is_resolvable_url


def is_google_drive_url(value: str) -> bool:
    host = urlparse(value).netloc.lower()
    return "drive.google.com" in host or "docs.google.com" in host


def is_telegram_url(value: str) -> bool:
    host = urlparse(value).netloc.lower()
    return host in {"t.me", "telegram.me"} or host.endswith(".t.me")


def looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"}


def yt_dlp_can_handle(value: str) -> bool:
    if not looks_like_url(value):
        return False
    try:
        with YoutubeDL({"quiet": True, "simulate": True, "skip_download": True}):
            return any(ie.suitable(value) and ie.IE_NAME != "generic" for ie in gen_extractors())
    except Exception:
        return False


def detect_source(value: str, filename: str = "") -> Source:
    if filename.lower().endswith(".torrent") or urlparse(value).path.lower().endswith(
        ".torrent"
    ):
        return Source(SourceType.TORRENT_FILE, value, filename)
    if value.startswith("magnet:"):
        return Source(SourceType.MAGNET, value)
    if is_google_drive_url(value):
        return Source(SourceType.GOOGLE_DRIVE, value)
    if is_telegram_url(value):
        return Source(SourceType.UNSUPPORTED, value)
    if is_resolvable_url(value):
        return Source(SourceType.DIRECT_URL, value)
    if yt_dlp_can_handle(value):
        return Source(SourceType.YTDLP, value)
    if looks_like_url(value):
        return Source(SourceType.DIRECT_URL, value)
    return Source(SourceType.UNSUPPORTED, value)
