import asyncio
import logging
from email.message import Message
from pathlib import Path
from time import monotonic
from urllib.parse import unquote, urlparse

import aiohttp

from ..models import Task
from ..resolvers.base import USER_AGENT, ResolvedCollection, safe_name

LOGGER = logging.getLogger(__name__)


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


async def download_direct(task: Task) -> Path:
    collection = task.source.metadata.get("collection")
    if isinstance(collection, ResolvedCollection):
        return await download_collection(task, collection)
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
    async with aiohttp.ClientSession(headers=headers, cookies=cookies) as session:
        async with session.get(task.source.value, allow_redirects=True) as response:
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
            started = monotonic()
            with target.open("wb") as file:
                async for chunk in response.content.iter_chunked(1024 * 512):
                    if task.cancelled:
                        raise asyncio.CancelledError()
                    file.write(chunk)
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
    started = monotonic()
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(3)
    base_headers = {"User-Agent": USER_AGENT, **(task.source.metadata.get("headers") or {})}
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

    async def download_item(item, target, session):
        if task.cancelled:
            raise asyncio.CancelledError()
        async with semaphore:
            target.parent.mkdir(parents=True, exist_ok=True)
            async with session.get(
                item.url,
                headers=item.headers,
                cookies=item.cookies,
                allow_redirects=True,
            ) as response:
                response.raise_for_status()
                with target.open("wb") as file:
                    async for chunk in response.content.iter_chunked(1024 * 512):
                        if task.cancelled:
                            raise asyncio.CancelledError()
                        file.write(chunk)
                        async with lock:
                            task.downloaded += len(chunk)
                            elapsed = monotonic() - started
                            task.speed = int(task.downloaded / elapsed) if elapsed else 0
                            if task.size:
                                task.progress = min(task.downloaded / task.size, 1)
                                task.eta = (
                                    int((task.size - task.downloaded) / task.speed)
                                    if task.speed
                                    else 0
                                )

    LOGGER.info(
        "Task %s: starting collection download name=%r files=%s",
        task.short_id(),
        task.name,
        len(collection.files),
    )
    async with aiohttp.ClientSession(headers=base_headers, cookies=base_cookies) as session:
        downloads = [
            asyncio.create_task(download_item(item, target, session))
            for item, target in zip(collection.files, targets)
        ]
        try:
            await asyncio.gather(*downloads)
        except BaseException:
            for download in downloads:
                download.cancel()
            await asyncio.gather(*downloads, return_exceptions=True)
            raise
    if not task.size:
        task.size = task.downloaded
    task.progress = 1
    task.eta = 0
    LOGGER.info(
        "Task %s: collection download complete name=%r files=%s bytes=%s",
        task.short_id(),
        task.name,
        len(collection.files),
        task.downloaded,
    )
    return root
