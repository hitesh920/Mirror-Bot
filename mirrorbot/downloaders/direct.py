import asyncio
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp

from ..models import Task


def filename_from_url(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).name
    return name or "download.bin"


async def download_direct(task: Task) -> Path:
    task.work_dir.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession() as session:
        async with session.get(task.source.value, allow_redirects=True) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", "0") or 0)
            filename = task.options.name or task.source.filename or filename_from_url(str(response.url))
            target = task.work_dir / filename
            task.name = filename
            task.size = total
            with target.open("wb") as file:
                async for chunk in response.content.iter_chunked(1024 * 512):
                    if task.cancelled:
                        raise asyncio.CancelledError()
                    file.write(chunk)
                    task.downloaded += len(chunk)
                    if total:
                        task.progress = task.downloaded / total
            return target
