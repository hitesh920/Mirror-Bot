from __future__ import annotations

import asyncio
import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from time import monotonic, time
from urllib.parse import unquote, urlsplit

import boto3
from botocore.config import Config as BotoConfig

from ..core.config import Config
from ..core.models import Task
from ..downloaders.process import path_size
from .paths import ensure_no_symlinks

LOGGER = logging.getLogger(__name__)
MULTIPART_THRESHOLD = 64 * 1024 * 1024
PART_SIZE = 32 * 1024 * 1024
EXPIRY_SWEEP_INTERVAL = 15 * 60


def normalize_prefix(value: str) -> str:
    prefix = value.strip().strip("/")
    return f"{prefix}/" if prefix else ""


def require_r2(config: Config) -> None:
    if not config.r2_configured:
        raise RuntimeError(
            "Cloudflare R2 is not configured. Set R2_ENDPOINT_URL, R2_BUCKET, "
            "R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY."
        )


def r2_client(config: Config):
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


async def cancellable_thread(function, /, *args, **kwargs):
    """Wait for an in-flight SDK thread before propagating cancellation."""
    job = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        try:
            await job
        except Exception:
            pass
        raise


def object_key(config: Config, task: Task, relative_name: str) -> str:
    relative = PurePosixPath(relative_name.replace("\\", "/"))
    safe_parts = [part for part in relative.parts if part not in {"", ".", "..", "/"}]
    if not safe_parts:
        safe_parts = ["file"]
    return f"{normalize_prefix(config.r2_prefix)}{task.id}/{'/'.join(safe_parts)}"


def iter_upload_files(path: Path) -> tuple[list[tuple[Path, str]], list[str]]:
    if path.is_file():
        return [(path, path.name)], []
    files = [
        (item, f"{path.name}/{item.relative_to(path).as_posix()}")
        for item in sorted(path.rglob("*"))
        if item.is_file()
    ]
    folders = [
        f"{path.name}/{item.relative_to(path).as_posix()}"
        for item in sorted(path.rglob("*"))
        if item.is_dir()
    ]
    return files, folders


def generate_download_url(client, config: Config, key: str) -> str:
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": config.r2_bucket, "Key": key},
        ExpiresIn=config.r2_link_expiry_seconds,
    )


class R2Uploader:
    def __init__(self, task: Task, path: Path, config: Config):
        require_r2(config)
        self.task = task
        self.path = path
        self.config = config
        self.client = r2_client(config)
        self.created_keys: list[str] = []
        self.active_upload: tuple[str, str] | None = None
        self.total_size = path_size(path)
        self.uploaded = 0
        self.started = monotonic()

    async def upload(self) -> None:
        ensure_no_symlinks(self.path)
        files, folders = iter_upload_files(self.path)
        self.task.size = self.total_size
        self.task.downloaded = 0
        self.task.progress = 0
        self.task.speed = 0
        self.task.eta = 0
        self.task.result_name = self.path.name
        self.task.result_files = []
        self.task.result_folders = folders
        self.task.result_links = []

        try:
            for file_path, display_name in files:
                if self.task.cancelled:
                    raise asyncio.CancelledError()
                key = object_key(self.config, self.task, display_name)
                self.task.current_file = display_name
                self.created_keys.append(key)
                await self.upload_file(file_path, key)
                self.task.result_files.append(display_name)
                self.task.result_links.append(
                    generate_download_url(self.client, self.config, key)
                )
            self.task.downloaded = self.total_size
            self.task.progress = 1
            self.task.eta = 0
            LOGGER.info(
                "Task %s: R2 upload complete files=%s bytes=%s",
                self.task.short_id(),
                len(files),
                self.total_size,
            )
        except asyncio.CancelledError:
            await self.cleanup_created("cancelled")
            raise
        except Exception:
            await self.cleanup_created("failed")
            raise

    async def upload_file(self, file_path: Path, key: str) -> None:
        size = file_path.stat().st_size
        content_type = (
            mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        )
        metadata = {
            "mirror-task": self.task.id,
            "expires-at": str(int(time()) + self.config.r2_auto_delete_seconds),
        }
        if size < MULTIPART_THRESHOLD:
            with file_path.open("rb") as body:
                await cancellable_thread(
                    self.client.put_object,
                    Bucket=self.config.r2_bucket,
                    Key=key,
                    Body=body,
                    ContentType=content_type,
                    Metadata=metadata,
                )
            self.uploaded += size
            self.update_progress()
            return

        response = await cancellable_thread(
            self.client.create_multipart_upload,
            Bucket=self.config.r2_bucket,
            Key=key,
            ContentType=content_type,
            Metadata=metadata,
        )
        upload_id = response["UploadId"]
        self.active_upload = (key, upload_id)
        parts = []
        try:
            with file_path.open("rb") as body:
                part_number = 1
                while chunk := body.read(PART_SIZE):
                    if self.task.cancelled:
                        raise asyncio.CancelledError()
                    uploaded = await cancellable_thread(
                        self.client.upload_part,
                        Bucket=self.config.r2_bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=part_number,
                        Body=chunk,
                    )
                    parts.append(
                        {
                            "ETag": uploaded["ETag"],
                            "PartNumber": part_number,
                        }
                    )
                    self.uploaded += len(chunk)
                    self.update_progress()
                    part_number += 1
            await cancellable_thread(
                self.client.complete_multipart_upload,
                Bucket=self.config.r2_bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
            self.active_upload = None
        except BaseException:
            await self.abort_active_upload()
            raise

    async def abort_active_upload(self) -> None:
        active = self.active_upload
        self.active_upload = None
        if active is None:
            return
        key, upload_id = active
        try:
            await asyncio.to_thread(
                self.client.abort_multipart_upload,
                Bucket=self.config.r2_bucket,
                Key=key,
                UploadId=upload_id,
            )
        except Exception:
            LOGGER.warning(
                "Task %s: could not abort R2 multipart upload",
                self.task.short_id(),
                exc_info=True,
            )

    async def cleanup_created(self, reason: str) -> None:
        await self.abort_active_upload()
        if not self.created_keys:
            return
        LOGGER.info(
            "Task %s: cleaning R2 objects count=%s reason=%s",
            self.task.short_id(),
            len(self.created_keys),
            reason,
        )
        try:
            await asyncio.to_thread(
                delete_keys,
                self.config,
                self.created_keys,
                self.client,
            )
        except Exception:
            LOGGER.warning(
                "Task %s: could not clean partial R2 objects",
                self.task.short_id(),
                exc_info=True,
            )

    def update_progress(self) -> None:
        processed = min(self.total_size, self.uploaded)
        self.task.downloaded = processed
        self.task.progress = processed / self.total_size if self.total_size else 1
        elapsed = monotonic() - self.started
        self.task.speed = int(processed / elapsed) if elapsed else 0
        self.task.eta = (
            int((self.total_size - processed) / self.task.speed)
            if self.total_size and self.task.speed
            else 0
        )


def list_objects(config: Config) -> list[dict]:
    client = r2_client(config)
    prefix = normalize_prefix(config.r2_prefix)
    objects: list[dict] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=config.r2_bucket, Prefix=prefix):
        objects.extend(page.get("Contents", []))
    return objects


def storage_stats(config: Config) -> dict[str, int]:
    objects = list_objects(config)
    return {
        "objects": len(objects),
        "bytes": sum(int(item.get("Size") or 0) for item in objects),
    }


def object_info(config: Config, key: str) -> dict:
    response = r2_client(config).head_object(Bucket=config.r2_bucket, Key=key)
    return {
        "key": key,
        "size": int(response.get("ContentLength") or 0),
        "modified": response.get("LastModified"),
    }


def search_objects(config: Config, query: str, limit: int = 100) -> list[dict]:
    needle = query.strip().casefold()
    if not needle:
        return []
    client = r2_client(config)
    results = [
        item
        for item in list_objects(config)
        if needle in PurePosixPath(item["Key"]).name.casefold()
    ]
    results.sort(key=lambda item: item["Key"].casefold())
    for item in results[:limit]:
        item["url"] = generate_download_url(client, config, item["Key"])
    return results[:limit]


def delete_keys(config: Config, keys: list[str], client=None) -> int:
    if not keys:
        return 0
    client = client or r2_client(config)
    deleted = 0
    for index in range(0, len(keys), 1000):
        batch = keys[index : index + 1000]
        response = client.delete_objects(
            Bucket=config.r2_bucket,
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
    return deleted


def delete_prefix(config: Config) -> int:
    return delete_keys(config, [item["Key"] for item in list_objects(config)])


def key_from_input(config: Config, value: str) -> str:
    raw = value.strip()
    if raw.startswith(("http://", "https://")):
        path = unquote(urlsplit(raw).path).lstrip("/")
        bucket_prefix = f"{config.r2_bucket}/"
        path = path.removeprefix(bucket_prefix)
        raw = path
    key = raw.lstrip("/")
    prefix = normalize_prefix(config.r2_prefix)
    if not key.startswith(prefix) or key == prefix:
        raise ValueError(f"R2 object must be inside the {prefix or 'configured'} prefix")
    return key


def delete_expired_objects(config: Config) -> int:
    if not config.r2_configured or config.r2_auto_delete_seconds <= 0:
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - config.r2_auto_delete_seconds
    keys = [
        item["Key"]
        for item in list_objects(config)
        if item["LastModified"].timestamp() <= cutoff
    ]
    return delete_keys(config, keys)


async def expiry_sweeper(config: Config) -> None:
    while True:
        try:
            deleted = await asyncio.to_thread(delete_expired_objects, config)
            if deleted:
                LOGGER.info("Deleted %s expired Cloudflare R2 object(s)", deleted)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.warning("Cloudflare R2 expiry sweep failed", exc_info=True)
        await asyncio.sleep(EXPIRY_SWEEP_INTERVAL)


async def upload_to_r2(task: Task, path: Path, config: Config) -> None:
    await R2Uploader(task, path, config).upload()
