"""Build verified public Google Drive share manifests."""

from dataclasses import dataclass

from ..core.config import Config
from .google_drive_delivery import FOLDER_MIME_TYPE, drive_link, drive_service

GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
PUBLIC_ROLES = {"reader", "commenter", "writer", "fileOrganizer", "organizer", "owner"}
ITEM_FIELDS = "id,name,mimeType,size,webViewLink,resourceKey,shortcutDetails"


class DriveShareError(RuntimeError):
    """Raised when a Drive item cannot be safely published."""


@dataclass(frozen=True)
class DriveShareFile:
    name: str
    folder_path: str
    url: str


@dataclass(frozen=True)
class DriveShareManifest:
    name: str
    is_folder: bool
    files: tuple[DriveShareFile, ...]
    folder_count: int


class DriveShareBuilder:
    def __init__(self, config: Config):
        self.service = drive_service(config)
        self.files: list[DriveShareFile] = []
        self.folder_count = 0
        self.visited_folders: set[str] = set()

    def item(self, file_id: str) -> dict:
        return (
            self.service.files()
            .get(fileId=file_id, supportsAllDrives=True, fields=ITEM_FIELDS)
            .execute()
        )

    def children(self, folder_id: str) -> list[dict]:
        page_token = None
        items = []
        while True:
            response = (
                self.service.files()
                .list(
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    q=f"'{folder_id}' in parents and trashed = false",
                    spaces="drive",
                    pageSize=200,
                    fields=f"nextPageToken,files({ITEM_FIELDS})",
                    orderBy="folder,name",
                    pageToken=page_token,
                )
                .execute()
            )
            items.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if page_token is None:
                return items

    def is_public(self, file_id: str) -> bool:
        page_token = None
        while True:
            response = (
                self.service.permissions()
                .list(
                    fileId=file_id,
                    supportsAllDrives=True,
                    fields="nextPageToken,permissions(type,role)",
                    pageToken=page_token,
                )
                .execute()
            )
            if any(
                permission.get("type") == "anyone"
                and permission.get("role") in PUBLIC_ROLES
                for permission in response.get("permissions", [])
            ):
                return True
            page_token = response.get("nextPageToken")
            if page_token is None:
                return False

    def resolve(self, item: dict) -> tuple[dict, str]:
        shortcut = item.get("shortcutDetails")
        if not shortcut:
            return item, item.get("name") or "Untitled"
        target = self.item(shortcut["targetId"])
        return target, item.get("name") or target.get("name") or "Untitled"

    def require_public(self, item: dict, display_name: str) -> None:
        if not self.is_public(item["id"]):
            raise DriveShareError(f"Not publicly accessible: {display_name}")

    def add_file(self, item: dict, display_name: str, folder_path: str) -> None:
        self.require_public(item, display_name)
        mime_type = item.get("mimeType", "")
        if mime_type.startswith(GOOGLE_NATIVE_PREFIX) and mime_type not in {
            FOLDER_MIME_TYPE,
            SHORTCUT_MIME_TYPE,
        }:
            url = item.get("webViewLink")
            if not url:
                raise DriveShareError(f"Public view link unavailable: {display_name}")
        else:
            url = drive_link(item["id"])
            if item.get("resourceKey"):
                url = f"{url}&resourcekey={item['resourceKey']}"
        self.files.append(DriveShareFile(display_name, folder_path, url))

    def walk_folder(self, folder: dict, folder_path: str) -> None:
        display_name = folder.get("name") or "Untitled"
        self.require_public(folder, display_name)
        if folder["id"] in self.visited_folders:
            raise DriveShareError(f"Folder shortcut cycle detected: {display_name}")
        self.visited_folders.add(folder["id"])
        self.folder_count += 1
        try:
            for child in self.children(folder["id"]):
                resolved, child_name = self.resolve(child)
                if resolved.get("mimeType") == FOLDER_MIME_TYPE:
                    child_path = f"{folder_path}/{child_name}" if folder_path else child_name
                    self.walk_folder(resolved, child_path)
                else:
                    self.add_file(resolved, child_name, folder_path)
        finally:
            self.visited_folders.remove(folder["id"])

    def build(self, file_id: str) -> DriveShareManifest:
        root, root_name = self.resolve(self.item(file_id))
        is_folder = root.get("mimeType") == FOLDER_MIME_TYPE
        if is_folder:
            self.walk_folder(root, root_name)
            if not self.files:
                raise DriveShareError("The Google Drive folder is empty.")
        else:
            self.add_file(root, root_name, "")
        return DriveShareManifest(
            root_name,
            is_folder,
            tuple(self.files),
            self.folder_count,
        )


def build_drive_share(config: Config, file_id: str) -> DriveShareManifest:
    return DriveShareBuilder(config).build(file_id)
