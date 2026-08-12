from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

from ...core.config import Config


def normalize_prefix(value: str, *, blank_default: bool = True) -> str:
    raw = str(value or "").strip()
    if not raw:
        if blank_default:
            return "uploads/"
        raise ValueError("R2 prefix cannot be empty")
    if "\\" in raw or any(ord(character) < 32 for character in raw):
        raise ValueError("R2 prefix contains unsupported characters")
    stripped = raw.strip("/")
    if not stripped:
        raise ValueError("R2 prefix cannot be the bucket root")
    parts = stripped.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("R2 prefix contains an unsafe path component")
    return f"{'/'.join(parts)}/"


def require_r2(config: Config) -> None:
    if not getattr(config, "r2_configured", False):
        raise RuntimeError(
            "Cloudflare R2 is not configured. Set R2_ENDPOINT_URL, R2_BUCKET, "
            "R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY."
        )


def create_r2_client(config: Config):
    require_r2(config)
    return boto3.client(
        "s3",
        endpoint_url=config.r2_endpoint_url,
        aws_access_key_id=config.r2_access_key_id,
        aws_secret_access_key=config.r2_secret_access_key,
        region_name="auto",
        config=BotoConfig(
            signature_version="s3v4",
            retries={"max_attempts": 4, "mode": "standard"},
            max_pool_connections=max(10, config.task_limit * 2),
            s3={"addressing_style": "path"},
        ),
    )


def _modified_timestamp(value: Any) -> float:
    return value.timestamp() if isinstance(value, datetime) else 0.0


@dataclass(frozen=True)
class ObjectIdentity:
    key: str
    etag: str
    modified_at: float
    size: int

    @classmethod
    def from_listing(cls, item: dict) -> ObjectIdentity:
        return cls(
            key=str(item["Key"]),
            etag=str(item.get("ETag") or ""),
            modified_at=_modified_timestamp(item.get("LastModified")),
            size=int(item.get("Size") or 0),
        )


class ActiveUploadRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._references: dict[str, int] = {}
        self._deleting: set[str] = set()

    def register(self, group: str) -> None:
        with self._lock:
            if group in self._deleting:
                raise RuntimeError("This R2 upload group is being deleted")
            self._references[group] = self._references.get(group, 0) + 1

    def unregister(self, group: str) -> None:
        with self._lock:
            count = self._references.get(group, 0)
            if count <= 1:
                self._references.pop(group, None)
            else:
                self._references[group] = count - 1

    def contains(self, group: str) -> bool:
        with self._lock:
            return group in self._references

    def snapshot(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._references)

    def reserve_inactive(self, groups) -> frozenset[str]:
        """Atomically prevent uploads from starting in deletable groups."""
        requested = set(groups)
        with self._lock:
            reserved = requested.difference(self._references, self._deleting)
            self._deleting.update(reserved)
            return frozenset(reserved)

    def release_deletions(self, groups) -> None:
        with self._lock:
            self._deleting.difference_update(groups)


class R2Service:
    """One managed R2 client, metadata cache, and upload safety registry."""

    def __init__(self, config: Config, client=None):
        if client is None:
            require_r2(config)
        self.config = config
        self.prefix = normalize_prefix(config.r2_prefix)
        self.bucket = config.r2_bucket
        self._client = client
        self._client_lock = threading.Lock()
        self._metadata_lock = threading.RLock()
        self._metadata_cache: dict[
            str,
            tuple[ObjectIdentity, dict[str, str]],
        ] = {}
        self._active = ActiveUploadRegistry()
        self._closed = False

    @property
    def client(self):
        with self._client_lock:
            if self._closed:
                raise RuntimeError("Cloudflare R2 service is closed")
            if self._client is None:
                self._client = create_r2_client(self.config)
            return self._client

    def close(self) -> None:
        with self._client_lock:
            if self._closed:
                return
            self._closed = True
            client = self._client
            self._client = None
        close = getattr(client, "close", None)
        if close is not None:
            close()

    def validate_key(self, key: str) -> str:
        candidate = str(key or "")
        if (
            not candidate.startswith(self.prefix)
            or candidate == self.prefix
            or "\\" in candidate
            or any(ord(character) < 32 for character in candidate)
        ):
            raise ValueError(f"R2 object must be inside the {self.prefix} prefix")
        relative = candidate.removeprefix(self.prefix)
        if any(part in {"", ".", ".."} for part in relative.split("/")):
            raise ValueError("R2 object key contains an unsafe path component")
        return candidate

    def validate_keys(self, keys) -> tuple[str, ...]:
        return tuple(self.validate_key(key) for key in keys)

    def task_group(self, key: str) -> str:
        validated = self.validate_key(key)
        task_id = validated.removeprefix(self.prefix).split("/", 1)[0]
        return f"{self.prefix}{task_id}/"

    def _validate_group(self, group: str) -> str:
        candidate = str(group or "")
        if not candidate.endswith("/"):
            candidate = f"{candidate}/"
        sample = f"{candidate}object"
        validated = self.task_group(sample)
        if validated != candidate:
            raise ValueError("R2 active-upload group is invalid")
        return candidate

    def register_active_group(self, group: str) -> None:
        self._active.register(self._validate_group(group))

    def unregister_active_group(self, group: str) -> None:
        self._active.unregister(self._validate_group(group))

    def is_group_active(self, group: str) -> bool:
        return self._active.contains(self._validate_group(group))

    @property
    def active_groups(self) -> frozenset[str]:
        return self._active.snapshot()

    def _validate_list_prefix(self, prefix: str | None) -> str:
        if prefix is None:
            return self.prefix
        candidate = str(prefix)
        if candidate == self.prefix:
            return candidate
        sample = candidate if not candidate.endswith("/") else f"{candidate}object"
        self.validate_key(sample)
        return candidate

    def list_objects(self, prefix: str | None = None) -> list[dict]:
        search_prefix = self._validate_list_prefix(prefix)
        objects: list[dict] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=search_prefix):
            objects.extend(page.get("Contents", []))
        return objects

    def metadata_for(self, item: dict) -> dict[str, str]:
        identity = ObjectIdentity.from_listing(item)
        self.validate_key(identity.key)
        with self._metadata_lock:
            cached = self._metadata_cache.get(identity.key)
            if cached is not None and cached[0] == identity:
                return dict(cached[1])
        response = self.client.head_object(Bucket=self.bucket, Key=identity.key)
        metadata = {
            str(key).casefold(): str(value)
            for key, value in response.get("Metadata", {}).items()
        }
        with self._metadata_lock:
            self._metadata_cache[identity.key] = (identity, metadata)
        return dict(metadata)

    def prune_metadata_cache(self, objects: list[dict]) -> None:
        present = {str(item["Key"]) for item in objects}
        with self._metadata_lock:
            for key in set(self._metadata_cache).difference(present):
                self._metadata_cache.pop(key, None)

    def _delete_validated_keys(self, keys) -> int:
        validated = self.validate_keys(keys)
        if not validated:
            return 0
        deleted = 0
        for index in range(0, len(validated), 1000):
            batch = validated[index : index + 1000]
            response = self.client.delete_objects(
                Bucket=self.bucket,
                Delete={
                    "Objects": [{"Key": key} for key in batch],
                    "Quiet": False,
                },
            )
            errors = response.get("Errors", [])
            if errors:
                raise RuntimeError(
                    f"Cloudflare R2 rejected {len(errors)} object deletion(s)"
                )
            deleted += len(response.get("Deleted", batch))
        with self._metadata_lock:
            for key in validated:
                self._metadata_cache.pop(key, None)
        return deleted

    def delete_keys(self, keys) -> int:
        validated = self.validate_keys(keys)
        groups = {self.task_group(key) for key in validated}
        reserved = self._active.reserve_inactive(groups)
        safe = [key for key in validated if self.task_group(key) in reserved]
        try:
            return self._delete_validated_keys(safe)
        finally:
            self._active.release_deletions(reserved)
