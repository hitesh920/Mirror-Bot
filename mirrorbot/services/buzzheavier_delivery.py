import asyncio
import logging
from pathlib import Path
from time import monotonic
from urllib.parse import quote

import aiohttp

from ..core.config import Config
from ..core.models import Task
from ..downloaders.process import path_size
from .telegram_delivery import upload_files

LOGGER = logging.getLogger(__name__)
UPLOAD_BASE = "https://w.buzzheavier.com"
UPLOAD_CHUNK_SIZE = 16 * 1024 * 1024


class BuzzHeavierUploadError(RuntimeError):
    pass


class BuzzHeavierUploader:
    def __init__(self, task: Task, path: Path, config: Config):
        self.task = task
        self.path = path
        self.config = config
        self.total_size = path_size(path)
        self.uploaded_base = 0
        self.started = monotonic()

    async def upload(self) -> None:
        files = upload_files(self.path)
        if not files:
            raise BuzzHeavierUploadError("Nothing to upload to BuzzHeavier")

        self.task.size = self.total_size
        self.task.downloaded = 0
        self.task.progress = 0
        self.task.speed = 0
        self.task.eta = 0
        self.task.result_name = self.path.name
        self.task.result_files = []
        self.task.result_folders = []
        self.task.result_links = []
        if self.path.is_dir():
            self.task.result_folders = [
                item.relative_to(self.path).as_posix()
                for item in sorted(self.path.rglob("*"))
                if item.is_dir()
            ]

        headers = {}
        if self.config.buzzheavier_account_id:
            headers["Authorization"] = f"Bearer {self.config.buzzheavier_account_id}"

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=600)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            for file_path, relative_name in files:
                if self.task.cancelled:
                    raise asyncio.CancelledError()
                link = await self._upload_one(session, file_path, relative_name)
                self.task.result_files.append(relative_name)
                self.task.result_links.append(link)

        self.task.downloaded = self.total_size
        self.task.progress = 1
        self.task.eta = 0
        LOGGER.info(
            "Task %s: BuzzHeavier upload complete files=%s bytes=%s",
            self.task.short_id(),
            len(self.task.result_files),
            self.total_size,
        )

    async def _upload_one(
        self, session: aiohttp.ClientSession, file_path: Path, relative_name: str
    ) -> str:
        file_size = file_path.stat().st_size
        self.task.current_file = relative_name
        LOGGER.info(
            "Task %s: uploading BuzzHeavier file name=%r size=%s",
            self.task.short_id(),
            relative_name,
            file_size,
        )
        upload_url = f"{UPLOAD_BASE}/{quote(file_path.name)}"
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(file_size),
        }
        async with session.put(
            upload_url,
            data=self._chunks(file_path, file_size),
            headers=headers,
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise BuzzHeavierUploadError(
                    f"BuzzHeavier upload failed with HTTP {response.status}"
                )
            try:
                payload = await response.json(content_type=None)
            except Exception as exc:
                raise BuzzHeavierUploadError("BuzzHeavier returned an invalid response") from exc
        file_id = (payload.get("data") or {}).get("id") or payload.get("id")
        if not file_id:
            raise BuzzHeavierUploadError("BuzzHeavier response did not include a file id")
        self.uploaded_base += file_size
        self._update_progress(0)
        LOGGER.debug(
            "Task %s: BuzzHeavier response body=%s",
            self.task.short_id(),
            text[:300],
        )
        return f"https://buzzheavier.com/{file_id}"

    async def _chunks(self, file_path: Path, file_size: int):
        sent = 0
        with file_path.open("rb") as file:
            while True:
                if self.task.cancelled:
                    raise asyncio.CancelledError()
                chunk = await asyncio.to_thread(file.read, UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                sent += len(chunk)
                self._update_progress(sent)
                yield chunk
        if file_size == 0:
            self._update_progress(0)

    def _update_progress(self, current_file_bytes: int) -> None:
        processed = min(self.total_size, self.uploaded_base + current_file_bytes)
        self.task.downloaded = processed
        self.task.progress = processed / self.total_size if self.total_size else 1
        elapsed = monotonic() - self.started
        self.task.speed = int(processed / elapsed) if elapsed else 0
        self.task.eta = (
            int((self.total_size - processed) / self.task.speed)
            if self.total_size and self.task.speed
            else 0
        )


async def upload_to_buzzheavier(task: Task, path: Path, config: Config) -> None:
    uploader = BuzzHeavierUploader(task, path, config)
    await uploader.upload()
