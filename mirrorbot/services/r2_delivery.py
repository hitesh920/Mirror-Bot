from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
from contextlib import suppress
from datetime import UTC, datetime
from email.header import decode_header
from hashlib import sha256
from html import escape
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from time import monotonic, time
from urllib.parse import unquote, urlsplit

import boto3
from botocore.config import Config as BotoConfig

from ..core.config import Config
from ..core.formatting import human_size
from ..core.models import Task
from ..downloaders.process import path_size
from .paths import ensure_no_symlinks

LOGGER = logging.getLogger(__name__)
MULTIPART_THRESHOLD = 64 * 1024 * 1024
PART_SIZE = 32 * 1024 * 1024
EXPIRY_SWEEP_INTERVAL = 60 * 60
R2_DELETE_WARNING_SECONDS = 12 * 60 * 60
PRESIGNED_URL_LIFETIME = 7 * 24 * 60 * 60
FOLDER_PAGE_SUFFIX = ".mirrorbot-folder.html"
WARNING_STATE_FILENAME = ".r2-delete-warnings.json"
FolderExpiryCache = dict[str, tuple[tuple[str, float], int | None]]
WarningState = dict[str, int]
FOLDER_LABEL_PATTERN = re.compile(
    r'(?P<prefix><li data-file-name="(?P<name>[^"]+)" '
    r'data-file-url="[^"]+"><a href="[^"]+">Download</a><span>)'
    r"(?P<label>.*?)"
    r"(?P<suffix></span><small>)"
)


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
        with suppress(Exception):
            await job
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
        ExpiresIn=PRESIGNED_URL_LIFETIME,
    )


def folder_page_key(config: Config, task: Task, name: str) -> str:
    safe_name = PurePosixPath(name.replace("\\", "/")).name or "folder"
    return (
        f"{normalize_prefix(config.r2_prefix)}{task.id}/{safe_name}{FOLDER_PAGE_SUFFIX}"
    )


def is_folder_page_key(key: str) -> bool:
    return key.endswith(FOLDER_PAGE_SUFFIX)


def display_name_for_key(key: str) -> str:
    name = PurePosixPath(key).name
    if is_folder_page_key(key):
        return name.removesuffix(FOLDER_PAGE_SUFFIX)
    return name


def decode_metadata_value(value: str) -> str:
    parts = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8"))
        else:
            parts.append(chunk)
    return "".join(parts)


def upload_metadata(
    config: Config,
    task: Task,
    download_url: str,
    kind: str,
) -> dict[str, str]:
    metadata = {
        "mirror-task": task.id,
        "mirror-kind": kind,
        "mirror-link": download_url,
    }
    if config.r2_auto_delete_seconds > 0:
        metadata["expires-at"] = str(int(time()) + config.r2_auto_delete_seconds)
    return metadata


def build_folder_page(
    folder_name: str,
    files: list[tuple[str, str, int]],
    retention_seconds: int,
) -> bytes:
    rows = []
    for display_name, url, size in files:
        file_name = display_name.replace("\\", "/").rsplit("/", 1)[-1]
        rows.append(
            f'<li data-file-name="{escape(file_name, quote=True)}" '
            f'data-file-url="{escape(url, quote=True)}">'
            f'<a href="{escape(url, quote=True)}">Download</a>'
            f"<span>{escape(file_name)}</span>"
            f"<small>{human_size(size)}</small>"
            "</li>"
        )
    retention = (
        f"{retention_seconds // 86400} day"
        f"{'s' if retention_seconds // 86400 != 1 else ''}"
        if retention_seconds > 0 and retention_seconds % 86400 == 0
        else "the configured retention period"
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{escape(folder_name)}</title>
<style>
body{{margin:0;background:#0f172a;color:#e2e8f0;font:16px system-ui,sans-serif}}
main{{max-width:900px;margin:auto;padding:32px 18px}}
h1{{margin:0 0 8px;font-size:1.7rem;overflow-wrap:anywhere}}
p{{color:#94a3b8;margin:0 0 24px}}
.toolbar{{display:flex;align-items:center;gap:12px;margin:0 0 18px}}
.toolbar button{{border:0;border-radius:8px;padding:9px 14px;background:#2563eb;
color:#fff;font:inherit;font-weight:600;cursor:pointer}}
.toolbar button:hover{{background:#1d4ed8}}.toolbar button:focus-visible{{outline:3px solid #60a5fa}}
#copy-status{{color:#94a3b8;font-size:.9rem}}
ul{{list-style:none;padding:0;margin:0;display:grid;gap:10px}}
li{{display:grid;grid-template-columns:auto 1fr auto;gap:14px;align-items:center;
background:#1e293b;border:1px solid #334155;border-radius:12px;padding:14px}}
a{{color:#fff;background:#2563eb;padding:8px 12px;border-radius:8px;text-decoration:none}}
span{{overflow-wrap:anywhere}}small{{color:#94a3b8}}
@media(max-width:600px){{li{{grid-template-columns:1fr}}}}
</style>
</head>
<body><main>
<h1>{escape(folder_name)}</h1>
<p>{len(files)} file(s) · Automatically deleted after {escape(retention)}</p>
<div class="toolbar">
<button id="copy-all" type="button">Copy all</button>
<span id="copy-status" role="status" aria-live="polite"></span>
</div>
<ul>{"".join(rows)}</ul>
</main>
<script>
const copyButton = document.getElementById("copy-all");
const copyStatus = document.getElementById("copy-status");

async function writeClipboard(text) {{
  if (navigator.clipboard && window.isSecureContext) {{
    await navigator.clipboard.writeText(text);
    return;
  }}
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  const copied = document.execCommand("copy");
  area.remove();
  if (!copied) throw new Error("Clipboard copy was rejected");
}}

copyButton.addEventListener("click", async () => {{
  const entries = Array.from(document.querySelectorAll("li[data-file-url]"));
  const text = entries.map(
    (item) => item.dataset.fileName + "\\n" + item.dataset.fileUrl
  ).join("\\n\\n");
  try {{
    await writeClipboard(text);
    copyButton.textContent = "Copied!";
    copyStatus.textContent = `${{entries.length}} file links copied`;
    setTimeout(() => {{
      copyButton.textContent = "Copy all";
      copyStatus.textContent = "";
    }}, 2200);
  }} catch (error) {{
    copyStatus.textContent = "Copy failed. Allow clipboard access and try again.";
  }}
}});
</script>
</body>
</html>"""
    return document.encode("utf-8")


def normalize_folder_page_labels(document: bytes) -> tuple[bytes, int]:
    text = document.decode("utf-8")
    changes = 0

    def basename_label(match: re.Match) -> str:
        nonlocal changes
        if match.group("label") == match.group("name"):
            return match.group(0)
        changes += 1
        return f"{match.group('prefix')}{match.group('name')}{match.group('suffix')}"

    normalized = FOLDER_LABEL_PATTERN.sub(basename_label, text)
    return normalized.encode("utf-8"), changes


def update_existing_folder_pages(config: Config) -> dict[str, int]:
    require_r2(config)
    client = r2_client(config)
    pages = [item for item in list_objects(config) if is_folder_page_key(item["Key"])]
    updated_pages = 0
    updated_labels = 0
    for item in pages:
        response = client.get_object(Bucket=config.r2_bucket, Key=item["Key"])
        body = response["Body"]
        try:
            document = body.read()
        finally:
            with suppress(Exception):
                body.close()
        normalized, changes = normalize_folder_page_labels(document)
        if not changes:
            continue

        metadata = dict(response.get("Metadata", {}))
        if config.r2_auto_delete_seconds > 0 and not metadata.get("expires-at"):
            metadata["expires-at"] = str(
                int(item["LastModified"].timestamp()) + config.r2_auto_delete_seconds
            )
        put_options = {
            "Bucket": config.r2_bucket,
            "Key": item["Key"],
            "Body": normalized,
            "ContentType": response.get(
                "ContentType",
                "text/html; charset=utf-8",
            ),
            "ContentDisposition": response.get("ContentDisposition", "inline"),
            "Metadata": metadata,
        }
        for field in (
            "CacheControl",
            "ContentEncoding",
            "ContentLanguage",
        ):
            if response.get(field):
                put_options[field] = response[field]
        client.put_object(**put_options)
        updated_pages += 1
        updated_labels += changes

    return {
        "scanned": len(pages),
        "updated": updated_pages,
        "labels": updated_labels,
    }


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
        self.task.result_auto_delete_seconds = self.config.r2_auto_delete_seconds
        self.task.result_is_folder = self.path.is_dir()

        try:
            uploaded_files: list[tuple[str, str, int]] = []
            for file_path, display_name in files:
                if self.task.cancelled:
                    raise asyncio.CancelledError()
                key = object_key(self.config, self.task, display_name)
                download_url = generate_download_url(
                    self.client,
                    self.config,
                    key,
                )
                self.task.current_file = display_name
                self.created_keys.append(key)
                await self.upload_file(file_path, key, download_url)
                self.task.result_files.append(display_name)
                uploaded_files.append(
                    (display_name, download_url, file_path.stat().st_size)
                )
            if self.path.is_dir():
                page_key = folder_page_key(
                    self.config,
                    self.task,
                    self.path.name,
                )
                page_url = generate_download_url(
                    self.client,
                    self.config,
                    page_key,
                )
                self.created_keys.append(page_key)
                page = build_folder_page(
                    self.path.name,
                    uploaded_files,
                    self.config.r2_auto_delete_seconds,
                )
                await cancellable_thread(
                    self.client.put_object,
                    Bucket=self.config.r2_bucket,
                    Key=page_key,
                    Body=page,
                    ContentType="text/html; charset=utf-8",
                    ContentDisposition="inline",
                    Metadata=upload_metadata(
                        self.config,
                        self.task,
                        page_url,
                        "folder",
                    ),
                )
                self.task.result_links = [page_url]
            else:
                self.task.result_links = (
                    [uploaded_files[0][1]] if uploaded_files else []
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

    async def upload_file(
        self,
        file_path: Path,
        key: str,
        download_url: str,
    ) -> None:
        size = file_path.stat().st_size
        content_type = (
            mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        )
        metadata = upload_metadata(
            self.config,
            self.task,
            download_url,
            "file",
        )
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


def list_objects(config: Config, prefix: str | None = None) -> list[dict]:
    client = r2_client(config)
    search_prefix = normalize_prefix(config.r2_prefix) if prefix is None else prefix
    objects: list[dict] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=config.r2_bucket,
        Prefix=search_prefix,
    ):
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


def _task_group_key(config: Config, key: str) -> str:
    prefix = normalize_prefix(getattr(config, "r2_prefix", ""))
    relative = key.removeprefix(prefix)
    task_id, separator, _ = relative.partition("/")
    return task_id if separator else key


def _task_objects(config: Config, objects: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for item in objects:
        group_key = _task_group_key(config, item["Key"])
        groups.setdefault(group_key, []).append(item)
    return groups


def _folder_summary(page: dict, objects: list[dict]) -> dict:
    summary = dict(page)
    contents = [item for item in objects if not is_folder_page_key(item["Key"])]
    summary["Size"] = sum(int(item.get("Size") or 0) for item in contents)
    summary["ObjectCount"] = len(contents)
    return summary


def _all_uploads(config: Config, objects: list[dict]) -> list[dict]:
    uploads: list[dict] = []
    for group in _task_objects(config, objects).values():
        folder_page = next(
            (item for item in group if is_folder_page_key(item["Key"])),
            None,
        )
        if folder_page is not None:
            uploads.append(_folder_summary(folder_page, group))
        else:
            uploads.extend(dict(item) for item in group)

    def modified_timestamp(item: dict) -> float:
        modified = item.get("LastModified")
        return modified.timestamp() if hasattr(modified, "timestamp") else 0

    uploads.sort(key=modified_timestamp, reverse=True)
    return uploads


def search_objects(
    config: Config,
    query: str,
    limit: int | None = 100,
) -> list[dict]:
    needle = query.strip().casefold()
    if not needle:
        return []
    client = r2_client(config)
    objects = list_objects(config)
    if needle == "*":
        results = _all_uploads(config, objects)
    else:
        groups = _task_objects(config, objects)
        results = [
            (
                _folder_summary(
                    item,
                    groups.get(_task_group_key(config, item["Key"]), [item]),
                )
                if is_folder_page_key(item["Key"])
                else dict(item)
            )
            for item in objects
            if needle in item["Key"].casefold()
        ]
        results.sort(
            key=lambda item: (
                not is_folder_page_key(item["Key"]),
                item["Key"].casefold(),
            )
        )

    selected = results if limit is None else results[: max(0, limit)]
    for item in selected:
        response = client.head_object(
            Bucket=config.r2_bucket,
            Key=item["Key"],
        )
        metadata = response.get("Metadata", {})
        item["url"] = decode_metadata_value(metadata.get("mirror-link", ""))
        item["kind"] = metadata.get("mirror-kind", "file")
        item["name"] = display_name_for_key(item["Key"])
    return selected


def delete_scope(config: Config, key: str) -> dict:
    if not is_folder_page_key(key):
        item = object_info(config, key)
        return {
            "keys": [key],
            "objects": 1,
            "bytes": item["size"],
            "name": display_name_for_key(key),
            "kind": "file",
        }
    prefix = normalize_prefix(config.r2_prefix)
    relative = key.removeprefix(prefix)
    task_id = relative.split("/", 1)[0]
    task_prefix = f"{prefix}{task_id}/"
    objects = list_objects(config, task_prefix)
    return {
        "keys": [item["Key"] for item in objects],
        "objects": len(objects),
        "bytes": sum(int(item.get("Size") or 0) for item in objects),
        "name": display_name_for_key(key),
        "kind": "folder",
    }


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
        raise ValueError(
            f"R2 object must be inside the {prefix or 'configured'} prefix"
        )
    return key


def _object_identity(item: dict) -> tuple[str, float]:
    modified = item.get("LastModified")
    modified_at = modified.timestamp() if hasattr(modified, "timestamp") else 0.0
    return str(item.get("ETag") or ""), modified_at


def _folder_page_expiry(
    config: Config,
    item: dict,
    cache: FolderExpiryCache,
    client,
) -> tuple[int | None, object]:
    key = item["Key"]
    identity = _object_identity(item)
    cached = cache.get(key)
    if cached is not None and cached[0] == identity:
        return cached[1], client

    client = client or r2_client(config)
    try:
        response = client.head_object(
            Bucket=config.r2_bucket,
            Key=key,
        )
        raw_expiry = response.get("Metadata", {}).get("expires-at", "")
        expires_at = int(raw_expiry) if raw_expiry else None
    except (TypeError, ValueError):
        LOGGER.warning("Invalid R2 folder page expiry metadata key=%s", key)
        expires_at = None
    except Exception:
        LOGGER.warning(
            "Could not inspect R2 folder page expiry key=%s",
            key,
            exc_info=True,
        )
        return None, client

    cache[key] = (identity, expires_at)
    return expires_at, client


def _warning_state_path(config: Config) -> Path:
    return Path(getattr(config, "log_file", "logs/bot.log")).parent / (
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
    identity = f"{upload['group']}:{int(upload['expires_at'])}"
    return sha256(identity.encode("utf-8")).hexdigest()


def expiring_uploads(
    config: Config,
    folder_expiry_cache: FolderExpiryCache | None = None,
    *,
    objects: list[dict] | None = None,
    now: float | None = None,
    warning_seconds: int = R2_DELETE_WARNING_SECONDS,
) -> list[dict]:
    """Return one upload-level warning for objects entering their final window."""
    if not config.r2_configured or config.r2_auto_delete_seconds <= 0:
        return []
    cache = folder_expiry_cache if folder_expiry_cache is not None else {}
    current_time = datetime.now(UTC).timestamp() if now is None else now
    current_objects = list_objects(config) if objects is None else objects
    folder_keys = {
        item["Key"] for item in current_objects if is_folder_page_key(item["Key"])
    }
    for key in set(cache).difference(folder_keys):
        cache.pop(key, None)

    warnings = []
    client = None
    for group_key, group in _task_objects(config, current_objects).items():
        folder_page = next(
            (item for item in group if is_folder_page_key(item["Key"])),
            None,
        )
        contents = [item for item in group if not is_folder_page_key(item["Key"])]
        if folder_page is not None:
            expires_at, client = _folder_page_expiry(
                config,
                folder_page,
                cache,
                client,
            )
            representative = folder_page
            kind = "folder"
            size = sum(int(item.get("Size") or 0) for item in contents)
            object_count = len(contents)
        else:
            dated_items = [
                item for item in group if hasattr(item.get("LastModified"), "timestamp")
            ]
            if not dated_items:
                continue
            representative = min(
                dated_items,
                key=lambda item: item["LastModified"].timestamp(),
            )
            expires_at = int(
                representative["LastModified"].timestamp()
                + config.r2_auto_delete_seconds
            )
            kind = "file"
            size = sum(int(item.get("Size") or 0) for item in group)
            object_count = len(group)

        if expires_at is None:
            modified = representative.get("LastModified")
            if not hasattr(modified, "timestamp"):
                continue
            expires_at = int(modified.timestamp() + config.r2_auto_delete_seconds)
        remaining = int(expires_at - current_time)
        if remaining <= 0 or remaining > warning_seconds:
            continue
        warnings.append(
            {
                "group": group_key,
                "key": representative["Key"],
                "name": display_name_for_key(representative["Key"]),
                "kind": kind,
                "objects": object_count,
                "bytes": size,
                "expires_at": int(expires_at),
                "remaining_seconds": remaining,
            }
        )
    warnings.sort(key=lambda item: (item["expires_at"], item["name"].casefold()))
    return warnings


def delete_expired_objects(
    config: Config,
    folder_expiry_cache: FolderExpiryCache | None = None,
    *,
    objects: list[dict] | None = None,
    now: float | None = None,
) -> int:
    if not config.r2_configured or config.r2_auto_delete_seconds <= 0:
        return 0
    cache = folder_expiry_cache if folder_expiry_cache is not None else {}
    current_time = datetime.now(UTC).timestamp() if now is None else now
    cutoff = current_time - config.r2_auto_delete_seconds
    client = None
    keys = []
    current_objects = list_objects(config) if objects is None else objects
    folder_keys = {
        item["Key"] for item in current_objects if is_folder_page_key(item["Key"])
    }
    for key in set(cache).difference(folder_keys):
        cache.pop(key, None)

    for item in current_objects:
        expired = item["LastModified"].timestamp() <= cutoff
        if is_folder_page_key(item["Key"]):
            expires_at, client = _folder_page_expiry(
                config,
                item,
                cache,
                client,
            )
            if expires_at is not None:
                expired = expires_at <= current_time
        if expired:
            keys.append(item["Key"])
    deleted = delete_keys(config, keys)
    if deleted:
        for key in keys:
            cache.pop(key, None)
    return deleted


async def run_expiry_sweep_once(
    config: Config,
    notify_warning=None,
    folder_expiry_cache: FolderExpiryCache | None = None,
    warning_state: WarningState | None = None,
    state_path: Path | None = None,
) -> dict[str, int]:
    cache = folder_expiry_cache if folder_expiry_cache is not None else {}
    state = warning_state if warning_state is not None else {}
    path = state_path or _warning_state_path(config)
    current_time = datetime.now(UTC).timestamp()
    objects = await asyncio.to_thread(list_objects, config)
    warnings = await asyncio.to_thread(
        expiring_uploads,
        config,
        cache,
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
                await notify_warning(upload)
            except asyncio.CancelledError:
                raise
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

    deleted = await asyncio.to_thread(
        delete_expired_objects,
        config,
        cache,
        objects=objects,
        now=current_time,
    )
    return {"warned": warned, "deleted": deleted}


async def expiry_sweeper(config: Config, notify_warning=None) -> None:
    folder_expiry_cache: FolderExpiryCache = {}
    state_path = _warning_state_path(config)
    warning_state = await asyncio.to_thread(load_warning_state, state_path)
    while True:
        try:
            result = await run_expiry_sweep_once(
                config,
                notify_warning,
                folder_expiry_cache,
                warning_state,
                state_path,
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


async def upload_to_r2(task: Task, path: Path, config: Config) -> None:
    await R2Uploader(task, path, config).upload()
