from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
from contextlib import suppress
from email.header import decode_header
from html import escape
from pathlib import Path, PurePosixPath
from time import monotonic, time
from typing import Any

from ...core.config import Config
from ...core.formatting import human_size
from ...core.models import Task
from ..paths import ensure_no_symlinks
from .client import R2Service, normalize_prefix
from .retention import format_retention

LOGGER = logging.getLogger(__name__)
MULTIPART_THRESHOLD = 64 * 1024 * 1024
PART_SIZE = 32 * 1024 * 1024
PRESIGNED_URL_LIFETIME = 7 * 24 * 60 * 60
FOLDER_PAGE_SUFFIX = ".mirrorbot-folder.html"
FOLDER_LABEL_PATTERN = re.compile(
    r'(?P<prefix><li data-file-name="(?P<name>[^"]+)" '
    r'data-file-url="[^"]+"><a href="[^"]+">Download</a><span>)'
    r"(?P<label>.*?)"
    r"(?P<suffix></span><small>)"
)


async def cancellable_thread(
    function,
    /,
    *args,
    _cancel_result_handler=None,
    **kwargs,
):
    """Finish an in-flight worker call before propagating any cancellation."""
    job = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while not job.done():
        try:
            await asyncio.shield(job)
        except asyncio.CancelledError:
            cancelled = True
    result = job.result()
    if not cancelled:
        return result
    if _cancel_result_handler is not None:
        cleanup = asyncio.create_task(_cancel_result_handler(result))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()
    raise asyncio.CancelledError()


def object_key(config: Config, task: Task, relative_name: str) -> str:
    relative = PurePosixPath(relative_name.replace("\\", "/"))
    safe_parts = [part for part in relative.parts if part not in {"", ".", "..", "/"}]
    if not safe_parts:
        safe_parts = ["file"]
    return f"{normalize_prefix(config.r2_prefix)}{task.id}/{'/'.join(safe_parts)}"


def iter_upload_files(path: Path) -> tuple[list[tuple[Path, str]], list[str]]:
    """Return a stable upload manifest. Call this helper in a worker thread."""
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
    if retention_seconds <= 0:
        retention_note = "Automatic deletion is disabled."
    else:
        retention_note = (
            f"Automatically deleted after {format_retention(retention_seconds)}."
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
.toolbar button:hover{{background:#1d4ed8}}
.toolbar button:focus-visible{{outline:3px solid #60a5fa}}
#copy-status{{color:#94a3b8;font-size:.9rem}}
ul{{list-style:none;padding:0;margin:0;display:grid;gap:10px}}
li{{display:grid;grid-template-columns:auto 1fr auto;gap:14px;align-items:center;
background:#1e293b;border:1px solid #334155;border-radius:12px;padding:14px}}
a{{color:#fff;background:#2563eb;padding:8px 12px;border-radius:8px;
text-decoration:none}}
span{{overflow-wrap:anywhere}}small{{color:#94a3b8}}
@media(max-width:600px){{li{{grid-template-columns:1fr}}}}
</style>
</head>
<body><main>
<h1>{escape(folder_name)}</h1>
<p>{len(files)} file(s) · {escape(retention_note)}</p>
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


def update_existing_folder_pages(service: R2Service) -> dict[str, int]:
    """Normalize legacy folder pages using the service's managed client."""
    pages = [item for item in service.list_objects() if is_folder_page_key(item["Key"])]
    updated_pages = 0
    updated_labels = 0
    for item in pages:
        key = service.validate_key(item["Key"])
        response = service.client.get_object(Bucket=service.bucket, Key=key)
        body = response["Body"]
        try:
            document = body.read()
        finally:
            with suppress(Exception):
                body.close()

        metadata = {
            str(name).casefold(): str(value)
            for name, value in response.get("Metadata", {}).items()
        }
        explicit_kind = metadata.get("mirror-kind", "").strip().casefold()
        if explicit_kind and explicit_kind != "folder":
            continue
        normalized, changes = normalize_folder_page_labels(document)
        if not changes:
            continue

        if service.config.r2_auto_delete_seconds > 0 and not metadata.get("expires-at"):
            metadata["expires-at"] = str(
                int(item["LastModified"].timestamp())
                + service.config.r2_auto_delete_seconds
            )
        put_options: dict[str, Any] = {
            "Bucket": service.bucket,
            "Key": key,
            "Body": normalized,
            "ContentType": response.get(
                "ContentType",
                "text/html; charset=utf-8",
            ),
            "ContentDisposition": response.get("ContentDisposition", "inline"),
            "Metadata": metadata,
        }
        for field in ("CacheControl", "ContentEncoding", "ContentLanguage"):
            if response.get(field):
                put_options[field] = response[field]
        service.client.put_object(**put_options)
        updated_pages += 1
        updated_labels += changes

    return {
        "scanned": len(pages),
        "updated": updated_pages,
        "labels": updated_labels,
    }


def _prepare_upload(
    path: Path,
) -> tuple[list[tuple[Path, str]], list[str], dict[Path, int], int, bool]:
    ensure_no_symlinks(path)
    files, folders = iter_upload_files(path)
    sizes = {file_path: file_path.stat().st_size for file_path, _ in files}
    return files, folders, sizes, sum(sizes.values()), path.is_dir()


def _put_file(client, *, path: Path, **options) -> Any:
    with path.open("rb") as body:
        return client.put_object(Body=body, **options)


def _read_part(path: Path, offset: int, size: int) -> bytes:
    with path.open("rb") as body:
        body.seek(offset)
        return body.read(size)


class R2Uploader:
    def __init__(self, task: Task, path: Path, service: R2Service):
        self.task = task
        self.path = path
        self.service = service
        self.config = service.config
        self.client = service.client
        self.created_keys: list[str] = []
        self.active_upload: tuple[str, str] | None = None
        self.total_size = 0
        self.uploaded = 0
        self.started = monotonic()
        self.group = service.task_group(object_key(self.config, task, path.name))
        self._registered = False

    async def upload(self) -> None:
        files, folders, sizes, total_size, is_folder = await cancellable_thread(
            _prepare_upload,
            self.path,
        )
        self.total_size = total_size
        self.task.size = total_size
        self.task.downloaded = 0
        self.task.progress = 0
        self.task.speed = 0
        self.task.eta = 0
        self.task.result_name = self.path.name
        self.task.result_files = []
        self.task.result_folders = folders
        self.task.result_links = []
        self.task.result_auto_delete_seconds = self.config.r2_auto_delete_seconds
        self.task.result_is_folder = is_folder

        self.service.register_active_group(self.group)
        self._registered = True
        try:
            uploaded_files: list[tuple[str, str, int]] = []
            for file_path, display_name in files:
                if self.task.cancelled:
                    raise asyncio.CancelledError()
                key = self.service.validate_key(
                    object_key(self.config, self.task, display_name)
                )
                download_url = generate_download_url(self.client, self.config, key)
                self.task.current_file = display_name
                self.created_keys.append(key)
                await self.upload_file(
                    file_path,
                    key,
                    download_url,
                    size=sizes[file_path],
                )
                self.task.result_files.append(display_name)
                uploaded_files.append((display_name, download_url, sizes[file_path]))
            if is_folder:
                page_key = self.service.validate_key(
                    folder_page_key(self.config, self.task, self.path.name)
                )
                page_url = generate_download_url(self.client, self.config, page_key)
                self.created_keys.append(page_key)
                page = build_folder_page(
                    self.path.name,
                    uploaded_files,
                    self.config.r2_auto_delete_seconds,
                )
                await cancellable_thread(
                    self.client.put_object,
                    Bucket=self.service.bucket,
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
            await self.complete_cleanup("cancelled")
            raise
        except Exception:
            await self.complete_cleanup("failed")
            raise
        finally:
            if self._registered:
                self.service.unregister_active_group(self.group)
                self._registered = False

    async def upload_file(
        self,
        file_path: Path,
        key: str,
        download_url: str,
        *,
        size: int | None = None,
    ) -> None:
        if size is None:
            size = await asyncio.to_thread(lambda: file_path.stat().st_size)
        content_type = (
            mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        )
        metadata = upload_metadata(self.config, self.task, download_url, "file")
        if size < MULTIPART_THRESHOLD:
            await cancellable_thread(
                _put_file,
                self.client,
                path=file_path,
                Bucket=self.service.bucket,
                Key=key,
                ContentType=content_type,
                Metadata=metadata,
            )
            self.uploaded += size
            self.update_progress()
            return

        async def abort_cancelled_creation(response) -> None:
            self.active_upload = (key, response["UploadId"])
            await self.abort_active_upload()

        response = await cancellable_thread(
            self.client.create_multipart_upload,
            Bucket=self.service.bucket,
            Key=key,
            ContentType=content_type,
            Metadata=metadata,
            _cancel_result_handler=abort_cancelled_creation,
        )
        upload_id = response["UploadId"]
        self.active_upload = (key, upload_id)
        parts = []
        offset = 0
        try:
            part_number = 1
            while chunk := await cancellable_thread(
                _read_part,
                file_path,
                offset,
                PART_SIZE,
            ):
                if self.task.cancelled:
                    raise asyncio.CancelledError()
                uploaded = await cancellable_thread(
                    self.client.upload_part,
                    Bucket=self.service.bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=chunk,
                )
                parts.append({"ETag": uploaded["ETag"], "PartNumber": part_number})
                offset += len(chunk)
                self.uploaded += len(chunk)
                self.update_progress()
                part_number += 1
            await cancellable_thread(
                self.client.complete_multipart_upload,
                Bucket=self.service.bucket,
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
        if active is None:
            return
        key, upload_id = active
        try:
            await cancellable_thread(
                self.client.abort_multipart_upload,
                Bucket=self.service.bucket,
                Key=key,
                UploadId=upload_id,
            )
            self.active_upload = None
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
            await cancellable_thread(
                self.service._delete_validated_keys,
                self.created_keys,
            )
        except Exception:
            LOGGER.warning(
                "Task %s: could not clean partial R2 objects",
                self.task.short_id(),
                exc_info=True,
            )

    async def complete_cleanup(self, reason: str) -> None:
        """Drain rollback even if the upload task is cancelled repeatedly."""
        cleanup = asyncio.create_task(self.cleanup_created(reason))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        await cleanup

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


async def upload_to_r2(task: Task, path: Path, service: R2Service) -> None:
    await R2Uploader(task, path, service).upload()
