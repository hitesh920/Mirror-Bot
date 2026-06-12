import asyncio
import logging
import re
from io import FileIO
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, urlparse

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from ..core.config import Config
from ..core.models import Task, TaskPhase
from ..resolvers.base import safe_name
from ..services.google_drive_delivery import FOLDER_MIME_TYPE, load_credentials
from ..services.transfer_guard import ensure_disk_space

LOGGER = logging.getLogger(__name__)
EXPORT_MAP = {
    "application/vnd.google-apps.document": {
        "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "ext": ".docx",
    },
    "application/vnd.google-apps.spreadsheet": {
        "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "ext": ".xlsx",
    },
    "application/vnd.google-apps.presentation": {
        "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "ext": ".pptx",
    },
    "application/vnd.google-apps.drawing": {"mime": "image/png", "ext": ".png"},
}


def drive_id_from_url(link: str) -> str:
    if link.startswith("http") is False and "/" not in link and "?" not in link:
        return link
    if match := re.search(r"/d/([-\w]+)", link):
        return match.group(1)
    if match := re.search(r"/folders/([-\w]+)", link):
        return match.group(1)
    parsed = urlparse(link)
    query = parse_qs(parsed.query)
    if query.get("id"):
        return query["id"][0]
    if parsed.netloc == "docs.google.com":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 3 and parts[1] == "d":
            return parts[2]
    raise ValueError("Google Drive file ID was not found in the link")


class GoogleDriveDownloader:
    def __init__(self, task: Task, config: Config):
        self.task = task
        self.config = config
        self.service = build(
            "drive",
            "v3",
            credentials=load_credentials(config),
            cache_discovery=False,
        )
        self.started = monotonic()
        self.processed_base = 0

    def metadata(self, file_id: str) -> dict:
        return (
            self.service.files()
            .get(
                fileId=file_id,
                supportsAllDrives=True,
                fields="id,name,mimeType,size",
            )
            .execute()
        )

    def folder_children(self, folder_id: str) -> list[dict]:
        page_token = None
        files = []
        while True:
            response = (
                self.service.files()
                .list(
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    q=f"'{folder_id}' in parents and trashed = false",
                    spaces="drive",
                    pageSize=200,
                    fields=(
                        "nextPageToken, files(id,name,mimeType,size,shortcutDetails)"
                    ),
                    orderBy="folder,name",
                    pageToken=page_token,
                )
                .execute()
            )
            files.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if page_token is None:
                return files

    def folder_size(self, folder_id: str) -> int:
        total = 0
        for item in self.folder_children(folder_id):
            if self.task.cancelled:
                raise asyncio.CancelledError()
            file_id, mime_type = self.resolve_shortcut(item)
            if mime_type == FOLDER_MIME_TYPE:
                total += self.folder_size(file_id)
            elif item.get("shortcutDetails"):
                total += int(self.metadata(file_id).get("size") or 0)
            else:
                total += int(item.get("size") or 0)
        return total

    @staticmethod
    def resolve_shortcut(item: dict) -> tuple[str, str]:
        shortcut = item.get("shortcutDetails")
        if shortcut:
            return shortcut["targetId"], shortcut["targetMimeType"]
        return item["id"], item.get("mimeType", "")

    def download(self) -> Path:
        file_id = drive_id_from_url(self.task.source.value)
        meta = self.metadata(file_id)
        self.task.name = safe_name(self.task.options.name or meta["name"], "Google Drive")
        self.task.transition(TaskPhase.DOWNLOADING)
        self.task.downloaded = 0
        self.task.progress = 0
        self.task.speed = 0
        self.task.eta = 0
        self.task.work_dir.mkdir(parents=True, exist_ok=True)

        try:
            if meta.get("mimeType") == FOLDER_MIME_TYPE:
                self.task.size = self.folder_size(file_id)
                ensure_disk_space(self.task.work_dir, self.task.size)
                target = self.task.work_dir / self.task.name
                target.mkdir(parents=True, exist_ok=True)
                self.download_folder(file_id, target)
                return target

            ensure_disk_space(self.task.work_dir, int(meta.get("size") or 0))
            target = self.download_file(
                file_id,
                self.task.work_dir,
                self.task.name,
                meta.get("mimeType", ""),
                int(meta.get("size") or 0),
            )
            return target
        except HttpError as exc:
            detail = str(exc)
            if "downloadQuotaExceeded" in detail:
                raise RuntimeError("Google Drive download quota exceeded") from exc
            if "File not found" in detail or "notFound" in detail:
                raise RuntimeError("Google Drive file not found") from exc
            raise

    def download_folder(self, folder_id: str, target_dir: Path) -> None:
        for item in self.folder_children(folder_id):
            if self.task.cancelled:
                raise asyncio.CancelledError()
            file_id, mime_type = self.resolve_shortcut(item)
            name = safe_name(item.get("name", ""), "Google Drive")
            if mime_type == FOLDER_MIME_TYPE:
                child_dir = target_dir / name
                child_dir.mkdir(parents=True, exist_ok=True)
                self.download_folder(file_id, child_dir)
            else:
                self.download_file(
                    file_id,
                    target_dir,
                    name,
                    mime_type,
                    int(item.get("size") or 0),
                )

    def download_file(
        self,
        file_id: str,
        target_dir: Path,
        name: str,
        mime_type: str,
        file_size: int,
    ) -> Path:
        export = EXPORT_MAP.get(mime_type)
        filename = safe_name(name, "Google Drive")
        if export:
            request = self.service.files().export_media(
                fileId=file_id,
                mimeType=export["mime"],
            )
            if not filename.lower().endswith(export["ext"]):
                filename = f"{filename}{export['ext']}"
        else:
            request = self.service.files().get_media(
                fileId=file_id,
                supportsAllDrives=True,
                acknowledgeAbuse=True,
            )
        if len(filename.encode()) > 255:
            suffix = Path(filename).suffix
            filename = f"{filename[: 250 - len(suffix)]}{suffix}"
        target = target_dir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        self.task.current_file = target.relative_to(self.task.work_dir).as_posix()
        if file_size and not self.task.size:
            self.task.size = file_size

        downloaded_before_file = self.processed_base
        with FileIO(target, "wb") as file:
            downloader = MediaIoBaseDownload(
                file,
                request,
                chunksize=100 * 1024 * 1024,
            )
            done = False
            while not done:
                if self.task.cancelled:
                    file.close()
                    target.unlink(missing_ok=True)
                    raise asyncio.CancelledError()
                status, done = downloader.next_chunk()
                current = int(status.total_size * status.progress()) if status else 0
                if status and status.total_size and not file_size:
                    file_size = int(status.total_size)
                    if self.task.size < downloaded_before_file + file_size:
                        self.task.size = downloaded_before_file + file_size
                self.update_progress(downloaded_before_file + current)
        self.processed_base = downloaded_before_file + (file_size or target.stat().st_size)
        self.update_progress(self.processed_base)
        return target

    def update_progress(self, processed: int) -> None:
        self.task.downloaded = processed
        self.task.progress = min(processed / self.task.size, 1) if self.task.size else 0
        elapsed = monotonic() - self.started
        self.task.speed = int(processed / elapsed) if elapsed else 0
        self.task.eta = (
            int((self.task.size - processed) / self.task.speed)
            if self.task.size and self.task.speed
            else 0
        )


async def download_gdrive(task: Task, config: Config) -> Path:
    downloader = GoogleDriveDownloader(task, config)
    result = await asyncio.to_thread(downloader.download)
    if task.cancelled:
        raise asyncio.CancelledError()
    task.downloaded = task.size or task.downloaded
    task.progress = 1
    task.eta = 0
    LOGGER.info("Task %s: Google Drive download complete path=%s", task.short_id(), result)
    return result
