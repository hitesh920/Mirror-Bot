import logging
from pathlib import Path
from shutil import rmtree

LOGGER = logging.getLogger(__name__)


def cleanup_abandoned_downloads(download_dir: Path) -> None:
    root = download_dir.resolve()
    if root == Path(root.anchor):
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
