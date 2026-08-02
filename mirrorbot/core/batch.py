import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .models import Source, SourceType
from .source_detector import detect_source

MAX_BATCH_LINKS = 20
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
TRAILING_URL_PUNCTUATION = ".,;:!?)]}"


@dataclass
class BatchCollection:
    sources: list[Source] = field(default_factory=list)
    skipped: int = 0
    missing_messages: int = 0


def _message_text(message) -> tuple[str, list]:
    if getattr(message, "text", None):
        return message.text, list(getattr(message, "entities", None) or [])
    if getattr(message, "caption", None):
        return message.caption, list(getattr(message, "caption_entities", None) or [])
    return "", []


def extract_message_urls(message) -> list[str]:
    """Return visible and hidden HTTP URLs in first-seen order."""
    text, entities = _message_text(message)
    candidates = [
        (match.start(), index, match.group(0).rstrip(TRAILING_URL_PUNCTUATION))
        for index, match in enumerate(URL_PATTERN.finditer(text))
    ]
    for index, entity in enumerate(entities, len(candidates)):
        url = getattr(entity, "url", None)
        if not url or not str(url).lower().startswith(("http://", "https://")):
            continue
        utf16_offset = max(0, int(getattr(entity, "offset", len(text)) or 0))
        prefix = text.encode("utf-16-le")[: utf16_offset * 2]
        offset = len(prefix.decode("utf-16-le", errors="ignore"))
        candidates.append((offset, index, str(url)))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [value for _, _, candidate in candidates if (value := candidate.strip())]


def collect_batch_sources(messages: list) -> BatchCollection:
    """Collect supported direct sources without replacing missing messages."""
    result = BatchCollection()
    seen = set()
    for message in messages:
        if message is None or getattr(message, "empty", False):
            result.skipped += 1
            result.missing_messages += 1
            continue
        urls = extract_message_urls(message)
        if not urls:
            result.skipped += 1
            continue
        for url in urls:
            if url in seen:
                result.skipped += 1
                continue
            seen.add(url)
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                result.skipped += 1
                continue
            source = detect_source(url)
            if source.type != SourceType.DIRECT_URL:
                result.skipped += 1
                continue
            if len(result.sources) >= MAX_BATCH_LINKS:
                result.skipped += 1
                continue
            source.metadata["batch_direct_only"] = True
            result.sources.append(source)
    return result
