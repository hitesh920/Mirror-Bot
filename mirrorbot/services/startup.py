import logging
from pathlib import Path
from shutil import rmtree

from .media_library import apply_media_permissions

LOGGER = logging.getLogger(__name__)


def cleanup_abandoned_downloads(download_dir: Path, local_download_root: Path) -> None:
    root = download_dir.resolve()
    local_root = local_download_root.resolve()
    if root == Path(root.anchor) or root == local_root or root in local_root.parents:
        raise RuntimeError(f"Unsafe temporary download directory: {root}")

    root.mkdir(parents=True, exist_ok=True)
    removed = 0
    for item in root.iterdir():
        if item.is_symlink() or item.is_file():
            item.unlink(missing_ok=True)
        elif item.is_dir():
            rmtree(item)
        removed += 1
    if removed:
        LOGGER.info("Removed %s abandoned download workspace(s)", removed)


def prepare_local_library(local_download_root: Path) -> None:
    movies = local_download_root / "movies"
    series = local_download_root / "series"
    movies.mkdir(parents=True, exist_ok=True)
    series.mkdir(parents=True, exist_ok=True)
    apply_media_permissions(local_download_root, movies)
    apply_media_permissions(local_download_root, series)
