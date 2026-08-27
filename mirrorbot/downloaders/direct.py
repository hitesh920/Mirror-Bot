import asyncio
import logging
from email.message import Message
from pathlib import Path
from shutil import rmtree
from time import monotonic
from urllib.parse import unquote, urlparse

import aiofiles
import aiohttp

from ..core.errors import NetworkError, NetworkTimeoutError
from ..core.models import Task
from ..resolvers.base import USER_AGENT, ResolvedCollection, safe_name
from ..services.transfer_guard import ensure_disk_space

LOGGER = logging.getLogger(__name__)

DIRECT_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(
    total=None, sock_connect=60, sock_read=600
)
DIRECT_DOWNLOAD_RETRIES = 2
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


def filename_from_url(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).name
    return name or "download.bin"


def filename_from_headers(response: aiohttp.ClientResponse) -> str:
    disposition = response.headers.get("content-disposition", "")
    if not disposition:
        return ""
    message = Message()
    message["content-disposition"] = disposition
    return Path(message.get_filename("") or "").name


def _network_timeout_error() -> NetworkTimeoutError:
    return NetworkTimeoutError("Network read timed out while downloading")


def retryable_direct_error(exc: Exception) -> bool:
    if isinstance(exc, NetworkTimeoutError):
        return True
    if isinstance(exc, aiohttp.ClientResponseError):
        return exc.status in RETRYABLE_HTTP_STATUSES
    return isinstance(exc, (aiohttp.ClientError, OSError, TimeoutError))


async def download_direct(task: Task) -> Path:
    collection = task.source.metadata.get("collection")
    if isinstance(collection, ResolvedCollection):
        return await download_collection(task, collection)
    return await download_single_direct_with_retries(task)


async def download_single_direct_with_retries(task: Task) -> Path:
    last_error: Exception | None = None
    for attempt in range(1, DIRECT_DOWNLOAD_RETRIES + 2):
        if attempt > 1:
            task.downloaded = 0
            task.progress = 0
            task.speed = 0
            task.eta = 0
            rmtree(task.work_dir, ignore_errors=True)
            LOGGER.warning(
                "Task %s: retrying direct download attempt=%s",
                task.short_id(),
                attempt - 1,
            )
            await asyncio.sleep(min(10, 2 ** (attempt - 1)))
        try:
            return await download_single_direct(task)
        except Exception as exc:
            if not retryable_direct_error(exc) or attempt > DIRECT_DOWNLOAD_RETRIES:
                raise
            last_error = exc
    if last_error:
        raise last_error
    raise RuntimeError("Direct download failed")


async def download_single_direct(task: Task) -> Path:
    task.work_dir.mkdir(parents=True, exist_ok=True)
    original_filename = filename_from_url(task.source.value)
    requested_name = safe_name(task.options.name) if task.options.name else ""
    task.name = requested_name or task.source.filename or original_filename
    LOGGER.info(
        "Task %s: starting direct download name=%r host=%s",
        task.short_id(),
        task.name,
        urlparse(task.source.value).netloc,
    )
    headers = {"User-Agent": USER_AGENT, **(task.source.metadata.get("headers") or {})}
    cookies = task.source.metadata.get("cookies") or {}
    async with (
        aiohttp.ClientSession(
            headers=headers,
            cookies=cookies,
            timeout=DIRECT_DOWNLOAD_TIMEOUT,
        ) as session,
        session.get(task.source.value, allow_redirects=True) as response,
    ):
        response.raise_for_status()
        total = int(response.headers.get("content-length", "0") or 0)
        filename = (
            requested_name
            or task.source.filename
            or filename_from_headers(response)
            or original_filename
            or filename_from_url(str(response.url))
        )
        filename = safe_name(filename, "download.bin")
        target = task.work_dir / filename
        task.name = filename
        task.size = total
        ensure_disk_space(target, total)
        started = monotonic()
        async with aiofiles.open(target, "wb") as file:
            try:
                async for chunk in response.content.iter_chunked(1024 * 512):
                    if task.cancelled:
                        raise asyncio.CancelledError()
                    await file.write(chunk)
                    task.downloaded += len(chunk)
                    elapsed = monotonic() - started
                    task.speed = int(task.downloaded / elapsed) if elapsed else 0
                    if total:
                        task.progress = task.downloaded / total
                        task.eta = (
                            int((total - task.downloaded) / task.speed)
                            if task.speed
                            else 0
                        )
            except TimeoutError as exc:
                raise _network_timeout_error() from exc
        LOGGER.info(
            "Task %s: direct download complete name=%r bytes=%s",
            task.short_id(),
            filename,
            task.downloaded,
        )
        if not task.size:
            task.size = task.downloaded
        task.progress = 1
        task.eta = 0
        return target


async def download_collection(task: Task, collection: ResolvedCollection) -> Path:
    requested_name = safe_name(task.options.name) if task.options.name else ""
    root = task.work_dir / (requested_name or collection.title or "collection")
    root.mkdir(parents=True, exist_ok=True)
    task.name = root.name
    task.size = collection.total_size
    ensure_disk_space(root, task.size)
    started = monotonic()
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(3)
    base_headers = {
        "User-Agent": USER_AGENT,
        **(task.source.metadata.get("headers") or {}),
    }
    base_cookies = task.source.metadata.get("cookies") or {}
    targets = []
    used_targets = set()
    for item in collection.files:
        relative = Path(item.path) / item.filename
        candidate = relative
        index = 2
        while str(candidate).lower() in used_targets:
            candidate = relative.with_name(
                f"{relative.stem} ({index}){relative.suffix}"
            )
            index += 1
        used_targets.add(str(candidate).lower())
        targets.append(root / candidate)

    async def record_bytes(delta: int) -> None:
        async with lock:
            task.downloaded += delta
            elapsed = monotonic() - started
            task.speed = int(task.downloaded / elapsed) if elapsed else 0
            if task.size:
                task.progress = min(task.downloaded / task.size, 1)
                task.eta = (
                    int((task.size - task.downloaded) / task.speed)
                    if task.speed
                    else 0
                )

    async def fetch_once(item, target, session) -> None:
        written = 0
        try:
            async with session.get(
                item.url,
                headers=item.headers,
                cookies=item.cookies,
                allow_redirects=True,
            ) as response:
                response.raise_for_status()
                async with aiofiles.open(target, "wb") as file:
                    async for chunk in response.content.iter_chunked(1024 * 512):
                        if task.cancelled:
                            raise asyncio.CancelledError()
                        await file.write(chunk)
                        written += len(chunk)
                        await record_bytes(len(chunk))
        except TimeoutError as exc:
            await record_bytes(-written)
            raise _network_timeout_error() from exc
        except BaseException:
            await record_bytes(-written)
            raise

    async def download_item(item, target, session) -> None:
        if task.cancelled:
            raise asyncio.CancelledError()
        async with semaphore:
            target.parent.mkdir(parents=True, exist_ok=True)
            for attempt in range(1, DIRECT_DOWNLOAD_RETRIES + 2):
                if attempt > 1:
                    target.unlink(missing_ok=True)
                    await asyncio.sleep(min(10, 2 ** (attempt - 1)))
                try:
                    await fetch_once(item, target, session)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if (
                        not retryable_direct_error(exc)
                        or attempt > DIRECT_DOWNLOAD_RETRIES
                    ):
                        raise
                    LOGGER.warning(
                        "Task %s: retrying collection item name=%r attempt=%s",
                        task.short_id(),
                        item.filename,
                        attempt,
                    )

    LOGGER.info(
        "Task %s: starting collection download name=%r files=%s",
        task.short_id(),
        task.name,
        len(collection.files),
    )
    async with aiohttp.ClientSession(
        headers=base_headers, cookies=base_cookies, timeout=DIRECT_DOWNLOAD_TIMEOUT
    ) as session:
        downloads = [
            asyncio.create_task(download_item(item, target, session))
            for item, target in zip(collection.files, targets, strict=True)
        ]
        results = await asyncio.gather(*downloads, return_exceptions=True)

    cancelled = [r for r in results if isinstance(r, asyncio.CancelledError)]
    if cancelled:
        raise asyncio.CancelledError()
    failures = [r for r in results if isinstance(r, BaseException)]
    succeeded = len(results) - len(failures)
    for item, result in zip(collection.files, results, strict=True):
        if isinstance(result, BaseException):
            LOGGER.warning(
                "Task %s: collection item failed name=%r error=%s",
                task.short_id(),
                item.filename,
                result,
            )
            task.processing_warnings.append(
                f"{item.filename}: {type(result).__name__}"
            )
    if not succeeded:
        raise NetworkError("Every file in the collection failed to download")

    task.size = sum(item.stat().st_size for item in root.rglob("*") if item.is_file())
    task.downloaded = task.size
    task.progress = 1
    task.eta = 0
    LOGGER.info(
        "Task %s: collection download complete name=%r files=%s/%s bytes=%s",
        task.short_id(),
        task.name,
        succeeded,
        len(collection.files),
        task.downloaded,
    )
    return root
