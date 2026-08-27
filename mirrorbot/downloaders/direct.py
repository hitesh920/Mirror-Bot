import asyncio
import logging
from collections.abc import Awaitable, Callable
from email.message import Message
from pathlib import Path
from shutil import rmtree
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
COLLECTION_DOWNLOAD_CONCURRENCY = 3
CHUNK_SIZE = 1024 * 512
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


async def _retrying(
    task: Task,
    label: str,
    attempt: Callable[[], Awaitable],
    *,
    before_retry: Callable[[int], Awaitable] | None = None,
):
    """Run ``attempt`` with DIRECT_DOWNLOAD_RETRIES retries on retryable errors."""
    for number in range(1, DIRECT_DOWNLOAD_RETRIES + 2):
        if number > 1:
            if before_retry is not None:
                await before_retry(number)
            LOGGER.warning(
                "Task %s: retrying %s attempt=%s", task.short_id(), label, number - 1
            )
            await asyncio.sleep(min(10, 2 ** (number - 1)))
        try:
            return await attempt()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not retryable_direct_error(exc) or number > DIRECT_DOWNLOAD_RETRIES:
                raise
    raise RuntimeError(f"{label} failed")  # unreachable: last attempt re-raises


async def _stream_to_file(
    response: aiohttp.ClientResponse,
    target: Path,
    task: Task,
    on_bytes: Callable[[int], Awaitable],
) -> int:
    """Write the response body to ``target``; call ``on_bytes(delta)`` per chunk.

    Returns the number of bytes written. Read timeouts become NetworkTimeoutError.
    """
    written = 0
    async with aiofiles.open(target, "wb") as file:
        try:
            async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                if task.cancelled:
                    raise asyncio.CancelledError()
                await file.write(chunk)
                written += len(chunk)
                await on_bytes(len(chunk))
        except TimeoutError as exc:
            raise _network_timeout_error() from exc
    return written


async def download_direct(task: Task) -> Path:
    collection = task.source.metadata.get("collection")
    if isinstance(collection, ResolvedCollection):
        return await download_collection(task, collection)
    return await download_single_direct_with_retries(task)


async def download_single_direct_with_retries(task: Task) -> Path:
    async def before_retry(_number: int) -> None:
        task.begin_progress()
        rmtree(task.work_dir, ignore_errors=True)

    return await _retrying(
        task,
        "direct download",
        lambda: download_single_direct(task),
        before_retry=before_retry,
    )


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
        ensure_disk_space(target, total)
        task.begin_progress(total)

        async def bump(delta: int) -> None:
            task.advance_progress(delta)

        await _stream_to_file(response, target, task, bump)
        LOGGER.info(
            "Task %s: direct download complete name=%r bytes=%s",
            task.short_id(),
            filename,
            task.downloaded,
        )
        task.report_progress(task.downloaded, complete=True)
        return target


def _collection_targets(root: Path, collection: ResolvedCollection) -> list[Path]:
    targets: list[Path] = []
    used: set[str] = set()
    for item in collection.files:
        relative = Path(item.path) / item.filename
        candidate = relative
        index = 2
        while str(candidate).lower() in used:
            candidate = relative.with_name(
                f"{relative.stem} ({index}){relative.suffix}"
            )
            index += 1
        used.add(str(candidate).lower())
        targets.append(root / candidate)
    return targets


async def download_collection(task: Task, collection: ResolvedCollection) -> Path:
    requested_name = safe_name(task.options.name) if task.options.name else ""
    root = task.work_dir / (requested_name or collection.title or "collection")
    root.mkdir(parents=True, exist_ok=True)
    task.name = root.name
    ensure_disk_space(root, collection.total_size)
    task.begin_progress(collection.total_size)
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(COLLECTION_DOWNLOAD_CONCURRENCY)
    base_headers = {
        "User-Agent": USER_AGENT,
        **(task.source.metadata.get("headers") or {}),
    }
    base_cookies = task.source.metadata.get("cookies") or {}
    targets = _collection_targets(root, collection)

    async def record_bytes(delta: int) -> None:
        async with lock:
            task.advance_progress(delta)

    async def fetch_once(item, target, session) -> None:
        streamed = 0

        async def on_bytes(delta: int) -> None:
            nonlocal streamed
            streamed += delta
            await record_bytes(delta)

        try:
            async with session.get(
                item.url,
                headers=item.headers,
                cookies=item.cookies,
                allow_redirects=True,
            ) as response:
                response.raise_for_status()
                await _stream_to_file(response, target, task, on_bytes)
        except BaseException:
            # Undo this attempt's contribution so a retry does not double-count.
            await record_bytes(-streamed)
            raise

    async def download_item(item, target, session) -> None:
        if task.cancelled:
            raise asyncio.CancelledError()
        async with semaphore:
            target.parent.mkdir(parents=True, exist_ok=True)

            async def before_retry(_number: int) -> None:
                target.unlink(missing_ok=True)

            await _retrying(
                task,
                f"collection item {item.filename!r}",
                lambda: fetch_once(item, target, session),
                before_retry=before_retry,
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

    if any(isinstance(r, asyncio.CancelledError) for r in results):
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

    final_size = sum(
        item.stat().st_size for item in root.rglob("*") if item.is_file()
    )
    task.report_progress(final_size, size=final_size, complete=True)
    LOGGER.info(
        "Task %s: collection download complete name=%r files=%s/%s bytes=%s",
        task.short_id(),
        task.name,
        succeeded,
        len(collection.files),
        task.downloaded,
    )
    return root
