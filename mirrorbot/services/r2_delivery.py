"""Compatibility façade for the split Cloudflare R2 services.

New runtime code owns one :class:`R2Service` and imports the focused upload,
catalog, and retention modules directly. These wrappers preserve the earlier
Config-based function interface for maintenance scripts and downstream
callers, while owning and closing one service per top-level call.

The Config-based API cannot participate in the runtime's active-upload
registry because it has no shared :class:`R2Service`. Callers which need the
active-upload deletion boundary must use the focused R2 modules with the
application's shared service.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace

from .r2 import upload as _upload_module
from .r2.catalog import (
    catalog_item,
    create_search_snapshot,
    prepare_delete_all,
    prepare_delete_scope,
)
from .r2.catalog import key_from_input as _key_from_input
from .r2.client import (
    R2Service,
    create_r2_client,
    normalize_prefix,
    require_r2,
)
from .r2.retention import (
    EXPIRY_SWEEP_INTERVAL,
    R2_DELETE_WARNING_SECONDS,
    WARNING_STATE_FILENAME,
    expiry_warning_token,
    load_warning_state,
    save_warning_state,
)
from .r2.retention import (
    delete_expired_objects as _delete_expired_objects,
)
from .r2.retention import (
    expiring_uploads as _expiring_uploads,
)
from .r2.retention import expiry_sweeper as _expiry_sweeper
from .r2.retention import run_expiry_sweep_once as _run_expiry_sweep_once
from .r2.upload import (
    FOLDER_PAGE_SUFFIX,
    MULTIPART_THRESHOLD,
    PART_SIZE,
    PRESIGNED_URL_LIFETIME,
    build_folder_page,
    cancellable_thread,
    decode_metadata_value,
    display_name_for_key,
    folder_page_key,
    generate_download_url,
    is_folder_page_key,
    iter_upload_files,
    normalize_folder_page_labels,
    object_key,
    upload_metadata,
)
from .r2.upload import R2Uploader as _R2Uploader
from .r2.upload import (
    update_existing_folder_pages as _update_existing_folder_pages,
)

FolderExpiryCache = dict
WarningState = dict[str, int]
_ACTIVE_SERVICE: ContextVar[tuple[int, R2Service] | None] = ContextVar(
    "r2_compat_service",
    default=None,
)


def r2_client(config):
    return create_r2_client(config)


class _MetadataClient:
    def head_object(self, **_kwargs):
        return {"Metadata": {}}

    def close(self) -> None:
        pass


def _compat_config(config):
    values = dict(vars(config))
    values.setdefault("r2_prefix", "uploads/")
    values.setdefault("r2_bucket", "mirror-bot")
    values.setdefault("r2_auto_delete_seconds", 0)
    values["r2_configured"] = True
    return SimpleNamespace(**values)


def _service(config, client=None) -> R2Service:
    compatible = _compat_config(config)
    owns_client = client is None
    if client is None:
        try:
            client = r2_client(config)
        except (AttributeError, RuntimeError):
            client = _MetadataClient()
    try:
        return R2Service(compatible, client)
    except BaseException:
        if owns_client:
            close = getattr(client, "close", None)
            if close is not None:
                close()
        raise


@contextmanager
def _service_scope(config, client=None):
    """Reuse a call-local service and close the outermost owned service."""
    active = _ACTIVE_SERVICE.get()
    if client is None and active is not None and active[0] == id(config):
        yield active[1]
        return

    service = _service(config, client)
    token = _ACTIVE_SERVICE.set((id(config), service))
    try:
        yield service
    finally:
        _ACTIVE_SERVICE.reset(token)
        service.close()


def list_objects(config, prefix: str | None = None) -> list[dict]:
    with _service_scope(config) as service:
        return service.list_objects(prefix)


def storage_stats(config) -> dict[str, int]:
    with _service_scope(config):
        objects = list_objects(config)
        return {
            "objects": len(objects),
            "bytes": sum(int(item.get("Size") or 0) for item in objects),
        }


def object_info(config, key: str) -> dict:
    with _service_scope(config) as service:
        validated = service.validate_key(key)
        response = service.client.head_object(Bucket=service.bucket, Key=validated)
        return {
            "key": validated,
            "size": int(response.get("ContentLength") or 0),
            "modified": response.get("LastModified"),
        }


def search_objects(config, query: str, limit: int | None = 100) -> list[dict]:
    with _service_scope(config) as service:
        objects = list_objects(config)
        service.list_objects = lambda _prefix=None: objects
        snapshot = create_search_snapshot(service, query)
        candidates = (
            snapshot.candidates if limit is None else snapshot.candidates[:limit]
        )
        return [catalog_item(service, item, group) for item, group in candidates]


def delete_scope(config, key: str) -> dict:
    with _service_scope(config) as service:
        objects = list_objects(config, service.task_group(key))
        service.list_objects = lambda _prefix=None: objects
        plan = prepare_delete_scope(service, key)
        return {
            "keys": list(plan.keys),
            "objects": plan.objects,
            "bytes": plan.bytes,
            "name": plan.name,
            "kind": plan.kind,
        }


def delete_keys(config, keys: list[str], client=None) -> int:
    with _service_scope(config, client) as service:
        return service._delete_validated_keys(keys)


def delete_prefix(config) -> int:
    with _service_scope(config) as service:
        plan = prepare_delete_all(service)
        # The Config compatibility API has no shared active-upload registry.
        # Validate the snapshot, but do not imply that an isolated registry
        # provides the runtime's active-upload protection.
        return service._delete_validated_keys(plan.keys)


def key_from_input(config, value: str) -> str:
    with _service_scope(config) as service:
        return _key_from_input(service, value)


def _restore_compat_cache(cache: dict | None, service: R2Service) -> None:
    """Restore opaque metadata-cache entries without retaining their service."""
    if cache is None:
        return
    for key, value in cache.items():
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], dict):
            service._metadata_cache[str(key)] = value


def _sync_compat_cache(cache: dict | None, service: R2Service, objects: list[dict]):
    if cache is None:
        return
    service.prune_metadata_cache(objects)
    cache.clear()
    cache.update(service._metadata_cache)


def _install_compat_io(service: R2Service, config, objects: list[dict]) -> None:
    """Keep monkeypatchable facade I/O on the current scoped service."""
    service.list_objects = lambda _prefix=None: objects
    service.delete_keys = lambda keys: delete_keys(config, list(keys))


def _install_warning_only_legacy_head_fallback(service: R2Service) -> None:
    """Support legacy warning-only clients that assert on unknown HEAD calls.

    This narrow fallback preserves the old lightweight warning fixture API.
    Destructive expiry paths retain the focused retention module's conservative
    behavior and never derive deletion eligibility after a failed HEAD.
    """
    metadata_for = service.metadata_for

    def compatible_metadata(item):
        try:
            return metadata_for(item)
        except AssertionError:
            return {}

    service.metadata_for = compatible_metadata


def expiring_uploads(
    config,
    folder_expiry_cache: dict | None = None,
    *,
    objects: list[dict] | None = None,
    now: float | None = None,
    warning_seconds: int = R2_DELETE_WARNING_SECONDS,
) -> list[dict]:
    if (
        not getattr(config, "r2_configured", False)
        or getattr(config, "r2_auto_delete_seconds", 0) <= 0
    ):
        return []
    with _service_scope(config) as service:
        _restore_compat_cache(folder_expiry_cache, service)
        current_objects = list_objects(config) if objects is None else objects
        _install_warning_only_legacy_head_fallback(service)
        result = _expiring_uploads(
            service,
            objects=current_objects,
            now=now,
            warning_seconds=warning_seconds,
        )
        _sync_compat_cache(folder_expiry_cache, service, current_objects)
        return result


def delete_expired_objects(
    config,
    folder_expiry_cache: dict | None = None,
    *,
    objects: list[dict] | None = None,
    now: float | None = None,
) -> int:
    if (
        not getattr(config, "r2_configured", False)
        or getattr(config, "r2_auto_delete_seconds", 0) <= 0
    ):
        return 0
    with _service_scope(config) as service:
        _restore_compat_cache(folder_expiry_cache, service)
        current_objects = list_objects(config) if objects is None else objects
        _install_compat_io(service, config, current_objects)
        result = _delete_expired_objects(service, objects=current_objects, now=now)
        _sync_compat_cache(folder_expiry_cache, service, current_objects)
        return result


async def run_expiry_sweep_once(
    config,
    notify_warning=None,
    folder_expiry_cache: dict | None = None,
    warning_state: WarningState | None = None,
    state_path: Path | None = None,
) -> dict[str, int]:
    with _service_scope(config) as service:
        _restore_compat_cache(folder_expiry_cache, service)
        objects = await asyncio.to_thread(list_objects, config)
        _install_compat_io(service, config, objects)
        result = await _run_expiry_sweep_once(
            service,
            notify_warning,
            warning_state,
            state_path,
        )
        _sync_compat_cache(folder_expiry_cache, service, objects)
        return result


async def expiry_sweeper(config, notify_warning=None) -> None:
    """Run the legacy Config-based sweeper with one managed service."""
    with _service_scope(config) as service:
        await _expiry_sweeper(service, notify_warning)


def update_existing_folder_pages(config) -> dict[str, int]:
    with _service_scope(config) as service:
        objects = list_objects(config)
        service.list_objects = lambda _prefix=None: objects
        return _update_existing_folder_pages(service)


class R2Uploader(_R2Uploader):
    def __init__(self, task, path, config):
        service = _service(config)
        try:
            super().__init__(task, path, service)
        except BaseException:
            service.close()
            raise

    async def upload(self) -> None:
        try:
            await super().upload()
        finally:
            self.service.close()

    async def upload_file(self, *args, **kwargs):
        _upload_module.MULTIPART_THRESHOLD = MULTIPART_THRESHOLD
        _upload_module.PART_SIZE = PART_SIZE
        if not hasattr(self, "service"):
            self.service = SimpleNamespace(bucket=self.config.r2_bucket)
        return await super().upload_file(*args, **kwargs)


async def upload_to_r2(task, path: Path, config) -> None:
    await R2Uploader(task, path, config).upload()


__all__ = [
    "EXPIRY_SWEEP_INTERVAL",
    "FOLDER_PAGE_SUFFIX",
    "MULTIPART_THRESHOLD",
    "PART_SIZE",
    "PRESIGNED_URL_LIFETIME",
    "R2_DELETE_WARNING_SECONDS",
    "WARNING_STATE_FILENAME",
    "R2Uploader",
    "build_folder_page",
    "cancellable_thread",
    "decode_metadata_value",
    "delete_expired_objects",
    "delete_keys",
    "delete_prefix",
    "delete_scope",
    "display_name_for_key",
    "expiring_uploads",
    "expiry_sweeper",
    "expiry_warning_token",
    "folder_page_key",
    "generate_download_url",
    "is_folder_page_key",
    "iter_upload_files",
    "key_from_input",
    "list_objects",
    "load_warning_state",
    "normalize_folder_page_labels",
    "normalize_prefix",
    "object_info",
    "object_key",
    "r2_client",
    "require_r2",
    "run_expiry_sweep_once",
    "save_warning_state",
    "search_objects",
    "storage_stats",
    "update_existing_folder_pages",
    "upload_metadata",
    "upload_to_r2",
]
