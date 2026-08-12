from __future__ import annotations

from dataclasses import dataclass
from email.header import decode_header
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .client import R2Service
from .upload import FOLDER_PAGE_SUFFIX

DEFAULT_SEARCH_PAGE_SIZE = 10
MAX_SEARCH_PAGE_SIZE = 20
MAX_SEARCH_METADATA_REQUESTS = 20


@dataclass(frozen=True)
class StorageSummary:
    objects: int
    bytes: int


@dataclass(frozen=True)
class SearchSnapshot:
    query: str
    candidates: tuple[tuple[dict, tuple[dict, ...]], ...]


@dataclass(frozen=True)
class SearchPage:
    items: tuple[dict, ...]
    page: int
    page_size: int
    total: int
    snapshot: SearchSnapshot

    @property
    def has_previous(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return (self.page + 1) * self.page_size < self.total


@dataclass(frozen=True)
class DeletePlan:
    keys: tuple[str, ...]
    name: str
    kind: str
    objects: int
    bytes: int


@dataclass(frozen=True)
class DeleteSummary:
    requested: int
    deleted: int
    skipped_active: int


def decode_metadata_value(value: str) -> str:
    parts = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8"))
        else:
            parts.append(chunk)
    return "".join(parts)


def display_name_for_key(key: str, kind: str = "file") -> str:
    name = PurePosixPath(key).name
    if kind == "folder" and name.endswith(FOLDER_PAGE_SUFFIX):
        return name.removesuffix(FOLDER_PAGE_SUFFIX)
    return name


def _modified_timestamp(item: dict) -> float:
    modified = item.get("LastModified")
    return modified.timestamp() if hasattr(modified, "timestamp") else 0.0


def group_objects(
    service: R2Service,
    objects: list[dict],
) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for item in objects:
        key = service.validate_key(str(item["Key"]))
        groups.setdefault(service.task_group(key), []).append(item)
    return groups


def classify_item(service: R2Service, item: dict) -> tuple[str, dict[str, str]]:
    """Classify via stored metadata; suffix is compatibility-only."""
    try:
        metadata = service.metadata_for(item)
    except Exception:
        return "unknown", {}
    explicit = str(metadata.get("mirror-kind") or "").strip().casefold()
    if explicit:
        return explicit, metadata
    if str(item["Key"]).endswith(FOLDER_PAGE_SUFFIX):
        return "folder", metadata
    return "file", metadata


def catalog_item(
    service: R2Service,
    item: dict,
    group: tuple[dict, ...] | list[dict],
) -> dict:
    kind, metadata = classify_item(service, item)
    result = dict(item)
    if kind == "folder":
        contents = [candidate for candidate in group if candidate["Key"] != item["Key"]]
        result["Size"] = sum(int(candidate.get("Size") or 0) for candidate in contents)
        result["ObjectCount"] = len(contents)
    else:
        result["ObjectCount"] = 1
    result["kind"] = kind
    result["url"] = decode_metadata_value(metadata.get("mirror-link", ""))
    result["name"] = display_name_for_key(str(item["Key"]), kind)
    return result


def storage_stats(service: R2Service) -> StorageSummary:
    objects = service.list_objects()
    return StorageSummary(
        objects=len(objects),
        bytes=sum(int(item.get("Size") or 0) for item in objects),
    )


def create_search_snapshot(
    service: R2Service,
    query: str,
    *,
    discovery_limit: int = MAX_SEARCH_METADATA_REQUESTS,
) -> SearchSnapshot:
    needle = query.strip().casefold()
    if not needle:
        return SearchSnapshot(query="", candidates=())
    objects = service.list_objects()
    service.prune_metadata_cache(objects)
    groups = group_objects(service, objects)
    candidates: list[tuple[dict, tuple[dict, ...]]] = []
    discovery_heads = 0
    for group in groups.values():
        ordered = sorted(group, key=lambda item: str(item["Key"]).casefold())
        matching = [
            item
            for item in ordered
            if needle == "*" or needle in str(item["Key"]).casefold()
        ]
        if not matching:
            continue

        # Folder pages are metadata-defined.  Inspect likely legacy pages first,
        # then the rest of a multi-object group, but cap discovery HEAD requests
        # so a wildcard search cannot fan out without bound.  If no page can be
        # proven, expose every matching object rather than hiding a user file
        # that merely happens to use the historical suffix.
        discovery = sorted(
            ordered,
            key=lambda item: (
                not str(item["Key"]).endswith(FOLDER_PAGE_SUFFIX),
                not str(item["Key"]).casefold().endswith((".html", ".htm")),
                str(item["Key"]).casefold(),
            ),
        )
        folder_page = None
        inspected = 0
        if len(ordered) > 1 or str(ordered[0]["Key"]).endswith(FOLDER_PAGE_SUFFIX):
            for item in discovery:
                if discovery_heads >= max(0, discovery_limit):
                    break
                discovery_heads += 1
                inspected += 1
                kind, _metadata = classify_item(service, item)
                if kind == "folder":
                    folder_page = item
                    break

        discovery_was_capped = inspected < len(discovery) and folder_page is None
        if folder_page is None and discovery_was_capped:
            likely_pages = [
                item
                for item in discovery
                if str(item["Key"]).endswith(FOLDER_PAGE_SUFFIX)
                or str(item["Key"]).casefold().endswith((".html", ".htm"))
            ]
            if len(likely_pages) == 1:
                folder_page = likely_pages[0]
        selected = [folder_page] if folder_page is not None else matching
        group_snapshot = tuple(group)
        candidates.extend((dict(item), group_snapshot) for item in selected)
    if needle == "*":
        candidates.sort(key=lambda entry: _modified_timestamp(entry[0]), reverse=True)
    else:
        candidates.sort(key=lambda entry: str(entry[0]["Key"]).casefold())
    return SearchSnapshot(query=needle, candidates=tuple(candidates))


def search_page(
    service: R2Service,
    query: str,
    *,
    page: int = 0,
    page_size: int = DEFAULT_SEARCH_PAGE_SIZE,
    snapshot: SearchSnapshot | None = None,
) -> SearchPage:
    bounded_size = max(1, min(int(page_size), MAX_SEARCH_PAGE_SIZE))
    bounded_page = max(0, int(page))
    current = snapshot or create_search_snapshot(
        service,
        query,
        discovery_limit=max(0, MAX_SEARCH_METADATA_REQUESTS - bounded_size),
    )
    start = bounded_page * bounded_size
    chosen = current.candidates[start : start + bounded_size]
    items = tuple(catalog_item(service, item, group) for item, group in chosen)
    return SearchPage(
        items=items,
        page=bounded_page,
        page_size=bounded_size,
        total=len(current.candidates),
        snapshot=current,
    )


def key_from_input(service: R2Service, value: str) -> str:
    raw = value.strip()
    if raw.startswith(("http://", "https://")):
        path = unquote(urlsplit(raw).path).lstrip("/")
        raw = path.removeprefix(f"{service.bucket}/")
    return service.validate_key(raw.lstrip("/"))


def prepare_delete_scope(service: R2Service, key: str) -> DeletePlan:
    target = service.validate_key(key)
    group_key = service.task_group(target)
    if service.is_group_active(group_key):
        raise RuntimeError("This R2 upload is still active and cannot be deleted")
    group = service.list_objects(group_key)
    item = next((candidate for candidate in group if candidate["Key"] == target), None)
    if item is None:
        raise FileNotFoundError(f"Cloudflare R2 object was not found: {target}")
    kind, _metadata = classify_item(service, item)
    if kind == "folder":
        keys = tuple(service.validate_key(candidate["Key"]) for candidate in group)
        return DeletePlan(
            keys=keys,
            name=display_name_for_key(target, "folder"),
            kind="folder",
            objects=len(keys),
            bytes=sum(int(candidate.get("Size") or 0) for candidate in group),
        )
    return DeletePlan(
        keys=(target,),
        name=display_name_for_key(target),
        kind="file" if kind == "file" else "unknown",
        objects=1,
        bytes=int(item.get("Size") or 0),
    )


def prepare_delete_all(service: R2Service) -> DeletePlan:
    objects = service.list_objects()
    selected = []
    for group_key, group in group_objects(service, objects).items():
        if not service.is_group_active(group_key):
            selected.extend(group)
    keys = service.validate_keys(item["Key"] for item in selected)
    return DeletePlan(
        keys=keys,
        name=service.prefix,
        kind="prefix",
        objects=len(keys),
        bytes=sum(int(item.get("Size") or 0) for item in selected),
    )


def execute_delete_plan(service: R2Service, plan: DeletePlan) -> DeleteSummary:
    validated = service.validate_keys(plan.keys)
    deleted = service.delete_keys(validated)
    return DeleteSummary(
        requested=len(validated),
        deleted=deleted,
        skipped_active=max(0, len(validated) - deleted),
    )
