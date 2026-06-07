import asyncio
import logging
import mimetypes
import pickle
from pathlib import Path
from time import monotonic

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from ..core.config import Config
from ..core.models import Task
from ..downloaders.process import path_size

LOGGER = logging.getLogger(__name__)
DRIVE_SCOPE = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def load_credentials(config: Config) -> Credentials:
    if not config.google_token_file.is_file():
        raise FileNotFoundError(f"Google token not found at {config.google_token_file}")
    with config.google_token_file.open("rb") as file:
        credentials = pickle.load(file)
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        with config.google_token_file.open("wb") as file:
            pickle.dump(credentials, file)
    if credentials is None or not credentials.valid:
        raise RuntimeError("Google Drive token is invalid. Regenerate token.pickle.")
    return credentials


def drive_service(config: Config):
    return build(
        "drive",
        "v3",
        credentials=load_credentials(config),
        cache_discovery=False,
    )


def drive_storage_quota(config: Config) -> dict:
    about = (
        drive_service(config)
        .about()
        .get(fields="storageQuota")
        .execute()
    )
    return about.get("storageQuota", {})


def drive_link(file_id: str, is_folder: bool = False) -> str:
    if is_folder:
        return f"https://drive.google.com/drive/folders/{file_id}"
    return f"https://drive.google.com/uc?id={file_id}&export=download"


class GoogleDriveUploader:
    def __init__(self, task: Task, path: Path, config: Config):
        self.task = task
        self.path = path
        self.config = config
        self.service = drive_service(config)
        self.created_ids: list[str] = []
        self.total_size = path_size(path)
        self.uploaded_base = 0
        self.started = monotonic()

    def create_folder(self, name: str, parent_id: str) -> str:
        metadata = {
            "name": name,
            "mimeType": FOLDER_MIME_TYPE,
            "description": "Uploaded by Mirror-Bot",
        }
        if parent_id:
            metadata["parents"] = [parent_id]
        response = (
            self.service.files()
            .create(
                body=metadata,
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        folder_id = response["id"]
        self.created_ids.append(folder_id)
        self.set_public_permission(folder_id)
        return folder_id

    def set_public_permission(self, file_id: str) -> None:
        self.service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
        ).execute()

    def delete_created(self) -> None:
        for file_id in reversed(self.created_ids):
            try:
                self.service.files().delete(
                    fileId=file_id,
                    supportsAllDrives=True,
                ).execute()
            except Exception:
                LOGGER.debug(
                    "Could not delete partial Google Drive upload id=%s",
                    file_id,
                    exc_info=True,
                )

    async def upload(self) -> None:
        if not self.config.google_drive_folder_id:
            raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID is not configured")
        self.task.size = self.total_size
        self.task.downloaded = 0
        self.task.progress = 0
        self.task.speed = 0
        self.task.eta = 0
        self.task.result_files = []
        self.task.result_folders = []
        self.task.result_links = []

        try:
            if self.path.is_file():
                file_id = await self.upload_file(
                    self.path,
                    self.path.name,
                    self.config.google_drive_folder_id,
                    self.path.name,
                )
                self.task.result_name = self.path.name
                self.task.result_files.append(self.path.name)
                self.task.result_links.append(drive_link(file_id))
            else:
                root_name = self.path.name
                root_id = await asyncio.to_thread(
                    self.create_folder,
                    root_name,
                    self.config.google_drive_folder_id,
                )
                self.task.result_name = root_name
                self.task.result_folders.append(root_name)
                self.task.result_links.append(drive_link(root_id, is_folder=True))
                await self.upload_folder(self.path, root_id)

            self.task.downloaded = self.total_size
            self.task.progress = 1
            self.task.eta = 0
            LOGGER.info(
                "Task %s: Google Drive upload complete files=%s folders=%s bytes=%s",
                self.task.short_id(),
                len(self.task.result_files),
                len(self.task.result_folders),
                self.total_size,
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(self.delete_created)
            raise

    async def upload_folder(self, folder: Path, parent_id: str) -> None:
        for item in sorted(folder.iterdir(), key=lambda path: (path.is_file(), path.name.lower())):
            if self.task.cancelled:
                raise asyncio.CancelledError()
            if item.is_dir():
                relative = item.relative_to(self.path).as_posix()
                folder_id = await asyncio.to_thread(
                    self.create_folder,
                    item.name,
                    parent_id,
                )
                self.task.result_folders.append(relative)
                await self.upload_folder(item, folder_id)
            elif item.is_file():
                relative = item.relative_to(self.path).as_posix()
                await self.upload_file(item, item.name, parent_id, relative)
                self.task.result_files.append(relative)

    async def upload_file(
        self,
        file_path: Path,
        file_name: str,
        parent_id: str,
        display_name: str,
    ) -> str:
        self.task.current_file = display_name
        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        metadata = {
            "name": file_name,
            "description": "Uploaded by Mirror-Bot",
            "mimeType": mime_type,
        }
        if parent_id:
            metadata["parents"] = [parent_id]
        media = MediaFileUpload(
            str(file_path),
            mimetype=mime_type,
            resumable=True,
            chunksize=100 * 1024 * 1024,
        )
        request = self.service.files().create(
            body=metadata,
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        )
        response = None
        file_size = file_path.stat().st_size
        while response is None:
            if self.task.cancelled:
                raise asyncio.CancelledError()
            status, response = await asyncio.to_thread(request.next_chunk)
            current = int(file_size * status.progress()) if status else file_size
            self.update_progress(current)
        file_id = response["id"]
        self.created_ids.append(file_id)
        await asyncio.to_thread(self.set_public_permission, file_id)
        self.uploaded_base += file_size
        self.update_progress(0)
        return file_id

    def update_progress(self, current_file_bytes: int) -> None:
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


async def upload_to_gdrive(task: Task, path: Path, config: Config) -> None:
    uploader = GoogleDriveUploader(task, path, config)
    await uploader.upload()
