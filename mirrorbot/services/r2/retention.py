from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import R2Service

LOGGER = logging.getLogger(__name__)
EXPIRY_SWEEP_INTERVAL = 60 * 60
R2_DELETE_WARNING_SECONDS = 12 * 60 * 60
WARNING_NOTIFICATION_TIMEOUT = 10
MAX_FOLDER_PAGE_DISCOVERY_HEADS = 20
WARNING_STATE_FILENAME = ".r2-delete-warnings.json"
FOLDER_PAGE_SUFFIX = ".mirrorbot-folder.html"
WarningState = dict[str, int]


def format_retention(seconds: int) -> str:
    if seconds <= 0:
        return "Disabled"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    for amount, unit in (
        (days, "day"),
        (hours, "hour"),
        (minutes, "minute"),
        (seconds, "second"),
    ):
        if amount:
            parts.append(f"{amount} {unit}{'s' if amount != 1 else ''}")
    return " ".join(parts)


def display_name_for_key(key: str, kind: str = "file") -> str:
    name = key.rsplit("/", 1)[-1]
    if kind == "folder" and name.endswith(FOLDER_PAGE_SUFFIX):
        return name.removesuffix(FOLDER_PAGE_SUFFIX)
    return name


def _timestamp(value) -> float | None:
    return value.timestamp() if hasattr(value, "timestamp") else None


def _groups(service: R2Service, objects: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for item in objects:
        key = service.validate_key(str(item["Key"]))
        groups.setdefault(service.task_group(key), []).append(item)
    return groups


def _metadata_kind(key: str, metadata: dict[str, str]) -> str:
    kind = str(metadata.get("mirror-kind") or "").casefold()
    if kind:
        return kind
    if key.endswith(FOLDER_PAGE_SUFFIX):
        return "folder"
    return "file"


def _expiry_from_metadata(metadata: dict[str, str]) -> tuple[bool, int | None]:
    raw = metadata.get("expires-at", "")
    if not raw:
        return True, None
    try:
        expires_at = int(raw)
    except (TypeError, ValueError):
        return False, None
    return (expires_at > 0), (expires_at if expires_at > 0 else None)


def _folder_discovery_order(group: list[dict]) -> list[dict]:
    """Prioritize legacy and likely top-level pages without trusting filenames."""

    def priority(item: dict) -> tuple[bool, int, bool, float, str]:
        key = str(item["Key"])
        modified = _timestamp(item.get("LastModified")) or 0.0
        return (
            not key.endswith(FOLDER_PAGE_SUFFIX),
            key.count("/"),
            not key.casefold().endswith((".html", ".htm")),
            -modified,
            key.casefold(),
        )

    return sorted(group, key=priority)


def _folder_unit(
    group_key: str,
    group: list[dict],
    page: dict,
    expires_at: int,
) -> dict:
    page_key = str(page["Key"])
    contents = [item for item in group if str(item["Key"]) != page_key]
    return {
        "group": group_key,
        "key": page_key,
        "keys": tuple(str(item["Key"]) for item in group),
        "name": display_name_for_key(page_key, "folder"),
        "kind": "folder",
        "objects": len(contents),
        "bytes": sum(int(item.get("Size") or 0) for item in contents),
        "expires_at": expires_at,
    }


def _inspect_group(
    service: R2Service,
    group_key: str,
    group: list[dict],
) -> list[dict]:
    """Resolve retention units; a folder page owns its complete task group."""
    if service.is_group_active(group_key):
        return []
    inspected: list[tuple[dict, dict[str, str], str, bool, int | None]] = []
    discovery_complete = True
    for index, item in enumerate(_folder_discovery_order(group)):
        if index >= MAX_FOLDER_PAGE_DISCOVERY_HEADS:
            discovery_complete = False
            break
        try:
            metadata = service.metadata_for(item)
        except Exception:
            LOGGER.warning(
                "Could not inspect R2 retention metadata key=%s",
                item.get("Key"),
                exc_info=True,
            )
            discovery_complete = False
            continue
        kind = _metadata_kind(str(item["Key"]), metadata)
        valid, stored_expiry = _expiry_from_metadata(metadata)
        if not valid:
            LOGGER.warning("Invalid R2 expiry metadata key=%s", item.get("Key"))
        inspected.append((item, metadata, kind, valid, stored_expiry))
        if kind == "folder":
            if not valid:
                return []
            modified = _timestamp(item.get("LastModified"))
            if stored_expiry is None and modified is None:
                return []
            expires_at = (
                stored_expiry
                if stored_expiry is not None
                else int(modified + service.config.r2_auto_delete_seconds)
            )
            return [_folder_unit(group_key, group, item, int(expires_at))]

    # An unread or undiscovered object could itself be the authoritative page.
    # Do not apply standalone-file retention unless every object was classified.
    if not discovery_complete or len(inspected) != len(group):
        return []

    units = []
    for item, _metadata, _kind, expiry_valid, stored_expiry in inspected:
        if not expiry_valid:
            continue
        modified = _timestamp(item.get("LastModified"))
        if stored_expiry is None and modified is None:
            continue
        expires_at = (
            stored_expiry
            if stored_expiry is not None
            else int(modified + service.config.r2_auto_delete_seconds)
        )
        units.append(
            {
                "group": group_key,
                "key": item["Key"],
                "keys": (item["Key"],),
                "name": display_name_for_key(item["Key"]),
                "kind": "file",
                "objects": 1,
                "bytes": int(item.get("Size") or 0),
                "expires_at": int(expires_at),
            }
        )
    return units


def retention_units(
    service: R2Service,
    *,
    objects: list[dict] | None = None,
) -> list[dict]:
    if service.config.r2_auto_delete_seconds <= 0:
        return []
    current_objects = service.list_objects() if objects is None else objects
    service.prune_metadata_cache(current_objects)
    units = []
    for group_key, group in _groups(service, current_objects).items():
        units.extend(_inspect_group(service, group_key, group))
    return units


def expiring_uploads(
    service: R2Service,
    *,
    objects: list[dict] | None = None,
    now: float | None = None,
    warning_seconds: int = R2_DELETE_WARNING_SECONDS,
) -> list[dict]:
    current_time = datetime.now(UTC).timestamp() if now is None else now
    warnings = []
    for unit in retention_units(service, objects=objects):
        remaining = int(unit["expires_at"] - current_time)
        if 0 < remaining <= warning_seconds:
            warnings.append({**unit, "remaining_seconds": remaining})
    warnings.sort(key=lambda item: (item["expires_at"], item["name"].casefold()))
    return warnings


def delete_expired_objects(
    service: R2Service,
    *,
    objects: list[dict] | None = None,
    now: float | None = None,
) -> int:
    current_time = datetime.now(UTC).timestamp() if now is None else now
    keys = [
        key
        for unit in retention_units(service, objects=objects)
        if unit["expires_at"] <= current_time
        for key in unit["keys"]
    ]
    return service.delete_keys(keys)


def warning_state_path(service: R2Service) -> Path:
    return Path(getattr(service.config, "log_file", "logs/bot.log")).parent / (
        WARNING_STATE_FILENAME
    )


def load_warning_state(path: Path) -> WarningState:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("warning state must be an object")
        return {
            str(token): int(expires_at)
            for token, expires_at in payload.items()
            if int(expires_at) > 0
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        LOGGER.warning("Could not read R2 deletion warning state", exc_info=True)
        return {}


def save_warning_state(path: Path, state: WarningState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}-",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        json.dump(state, temporary, sort_keys=True, separators=(",", ":"))
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.chmod(temporary_path, 0o600)
    temporary_path.replace(path)


def expiry_warning_token(upload: dict) -> str:
    identity = f"{upload['key']}:{int(upload['expires_at'])}"
    return sha256(identity.encode("utf-8")).hexdigest()


async def _persist_warning_state_cancellation_safe(
    path: Path,
    state: WarningState,
) -> None:
    """Finish the atomic state write even if shutdown cancellation repeats."""
    persistence = asyncio.create_task(
        asyncio.to_thread(save_warning_state, path, dict(state))
    )
    while not persistence.done():
        try:
            await asyncio.shield(persistence)
        except asyncio.CancelledError:
            continue
    try:
        persistence.result()
    except Exception:
        LOGGER.warning(
            "Could not persist R2 deletion warning state",
            exc_info=True,
        )


async def run_expiry_sweep_once(
    service: R2Service,
    notify_warning: Callable[[dict], Awaitable[None]] | None = None,
    warning_state: WarningState | None = None,
    state_path: Path | None = None,
) -> dict[str, int]:
    state = warning_state if warning_state is not None else {}
    path = state_path or warning_state_path(service)
    current_time = datetime.now(UTC).timestamp()
    objects = await asyncio.to_thread(service.list_objects)

    # Expiration is schedule-driven. Telegram delivery is best effort and must
    # never delay deletion of objects whose stored deadline has passed.
    deleted = await asyncio.to_thread(
        delete_expired_objects,
        service,
        objects=objects,
        now=current_time,
    )
    warnings = await asyncio.to_thread(
        expiring_uploads,
        service,
        objects=objects,
        now=current_time,
    )

    state_changed = False
    for token, expires_at in list(state.items()):
        if expires_at <= current_time:
            state.pop(token, None)
            state_changed = True

    warned = 0
    if notify_warning is not None:
        for upload in warnings:
            token = expiry_warning_token(upload)
            if token in state:
                continue
            try:
                await asyncio.wait_for(
                    notify_warning(upload),
                    timeout=WARNING_NOTIFICATION_TIMEOUT,
                )
            except asyncio.CancelledError:
                if state_changed:
                    await _persist_warning_state_cancellation_safe(path, state)
                raise
            except TimeoutError:
                LOGGER.warning(
                    "R2 deletion warning timed out key=%s",
                    upload["key"],
                )
                continue
            except Exception:
                LOGGER.warning(
                    "Could not send R2 deletion warning key=%s",
                    upload["key"],
                    exc_info=True,
                )
                continue
            state[token] = upload["expires_at"]
            state_changed = True
            warned += 1

    if state_changed:
        try:
            await asyncio.to_thread(save_warning_state, path, state)
        except Exception:
            LOGGER.warning(
                "Could not persist R2 deletion warning state",
                exc_info=True,
            )
    return {"warned": warned, "deleted": deleted}


async def expiry_sweeper(
    service: R2Service,
    notify_warning: Callable[[dict], Awaitable[None]] | None = None,
) -> None:
    path = warning_state_path(service)
    state = await asyncio.to_thread(load_warning_state, path)
    while True:
        try:
            result = await run_expiry_sweep_once(
                service,
                notify_warning,
                state,
                path,
            )
            if result["warned"]:
                LOGGER.info(
                    "Sent %s Cloudflare R2 deletion warning(s)", result["warned"]
                )
            if result["deleted"]:
                LOGGER.info(
                    "Deleted %s expired Cloudflare R2 object(s)", result["deleted"]
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.warning("Cloudflare R2 expiry sweep failed", exc_info=True)
        await asyncio.sleep(EXPIRY_SWEEP_INTERVAL)
