import asyncio
import base64
import binascii
import logging
from pathlib import Path
from shutil import rmtree
from urllib.parse import parse_qs, urlparse

from ..core.models import Task, TaskPhase
from ..services.paths import ensure_inside
from ..services.transfer_guard import ensure_disk_space
from .qbittorrent import QBittorrentClient
from .torrent_selector import TorrentSelector

LOGGER = logging.getLogger(__name__)
FINISHED_STATES = {"uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP"}
ERROR_STATES = {"error", "missingFiles", "unknown"}
TORRENT_METADATA_TIMEOUT = 300


class DuplicateTorrentError(RuntimeError):
    pass


def magnet_info_hash(magnet: str) -> str:
    for value in parse_qs(urlparse(magnet).query).get("xt", []):
        if not value.lower().startswith("urn:btih:"):
            continue
        info_hash = value.rsplit(":", 1)[-1]
        if len(info_hash) == 40:
            return info_hash.lower()
        if len(info_hash) == 32:
            try:
                return base64.b32decode(info_hash.upper()).hex()
            except binascii.Error:
                return ""
    return ""


def _clean_skipped_files(task: Task, torrent: dict, files: list[dict]) -> None:
    save_path = Path(torrent["save_path"])
    ensure_inside(task.work_dir, save_path)
    for file in files:
        if file.get("priority", 0) != 0:
            continue
        target = save_path / file["name"]
        ensure_inside(task.work_dir, target)
        for candidate in (target, Path(f"{target}.!qB")):
            if candidate.is_file():
                candidate.unlink()
    unwanted = save_path / ".unwanted"
    ensure_inside(task.work_dir, unwanted)
    if unwanted.exists():
        rmtree(unwanted, ignore_errors=True)


async def _wait_for_torrent(
    qb: QBittorrentClient, task: Task, expected_hash: str = ""
) -> dict:
    for _ in range(60):
        if task.cancelled:
            raise asyncio.CancelledError()
        torrents = await qb.info(tag=task.id)
        if torrents:
            return torrents[0]
        if expected_hash:
            existing = await qb.info(torrent_hash=expected_hash)
            if existing and task.id not in {
                tag.strip()
                for tag in str(existing[0].get("tags", "")).split(",")
                if tag.strip()
            }:
                raise DuplicateTorrentError(
                    "This torrent is already active in qBittorrent"
                )
        await asyncio.sleep(1)
    raise TimeoutError("qBittorrent did not add the torrent")


async def _wait_for_metadata(qb: QBittorrentClient, task: Task) -> tuple[dict, list[dict]]:
    for _ in range(TORRENT_METADATA_TIMEOUT):
        if task.cancelled:
            raise asyncio.CancelledError()
        torrents = await qb.info(torrent_hash=task.torrent_hash)
        if not torrents:
            raise RuntimeError("Torrent disappeared while fetching metadata")
        torrent = torrents[0]
        task.progress = float(torrent.get("progress", 0))
        task.downloaded = int(torrent.get("downloaded", 0))
        task.size = int(torrent.get("size", task.size))
        task.speed = int(torrent.get("dlspeed", 0))
        task.eta = int(torrent.get("eta", 0))
        files = await qb.files(task.torrent_hash)
        if files and torrent.get("state") not in {"metaDL", "checkingResumeData"}:
            return torrent, files
        await asyncio.sleep(1)
    raise TimeoutError(
        "Torrent metadata download timed out after 5 minutes. "
        "The torrent may be dead or unavailable."
    )


async def download_torrent(
    task: Task,
    qb: QBittorrentClient,
    selector: TorrentSelector,
    torrent_file: Path | None = None,
    on_selector_ready=None,
    on_selector_done=None,
) -> Path:
    task.work_dir.mkdir(parents=True, exist_ok=True)
    source = torrent_file or task.source.value
    info_hash = ""
    if isinstance(source, str):
        info_hash = magnet_info_hash(source)
        if info_hash and await qb.info(torrent_hash=info_hash):
            raise DuplicateTorrentError("This torrent is already active in qBittorrent")
    await qb.add(source, task.work_dir, task.id)
    torrent = await _wait_for_torrent(qb, task, info_hash)
    task.torrent_hash = torrent["hash"]
    task.name = task.options.name or torrent["name"]
    task.transition(TaskPhase.METADATA)
    LOGGER.info("Task %s: torrent added hash=%s", task.short_id(), task.torrent_hash[:8])

    torrent, files = await _wait_for_metadata(qb, task)
    await qb.stop(task.torrent_hash)
    task.transition(TaskPhase.SELECTING)
    task.size = sum(file.get("size", 0) for file in files)

    selection_job = asyncio.create_task(selector.select(task.torrent_hash, files))
    while (
        selector.selection is None
        or selector.selection.torrent_hash != task.torrent_hash
    ):
        if task.cancelled:
            selection_job.cancel()
            try:
                await selection_job
            except asyncio.CancelledError:
                pass
            await qb.delete(task.torrent_hash, True)
            raise asyncio.CancelledError()
        await asyncio.sleep(0.2)
    task.selection_url = (
        f"{selector.public_base_url}/select/{selector.selection.token}"
    )
    try:
        selector_message = await on_selector_ready(task) if on_selector_ready else None
    except Exception:
        await selector.cancel(task.torrent_hash)
        await selection_job
        await qb.delete(task.torrent_hash, True)
        raise
    try:
        while not selection_job.done():
            if task.cancelled:
                await selector.cancel(task.torrent_hash)
                await selection_job
                await qb.delete(task.torrent_hash, True)
                raise asyncio.CancelledError()
            await asyncio.sleep(1)
        await selection_job
        task.transition(TaskPhase.DOWNLOADING)
        selected_size = sum(file.get("size", 0) for file in files if file.get("priority", 0) != 0)
        ensure_disk_space(task.work_dir, selected_size)
    finally:
        if on_selector_done and selector_message:
            await on_selector_done(selector_message)

    while True:
        if task.cancelled:
            await qb.delete(task.torrent_hash, True)
            raise asyncio.CancelledError()
        torrents = await qb.info(torrent_hash=task.torrent_hash)
        if not torrents:
            raise RuntimeError("Torrent disappeared from qBittorrent")
        torrent = torrents[0]
        task.progress = float(torrent.get("progress", 0))
        task.downloaded = int(torrent.get("downloaded", 0))
        task.size = int(torrent.get("size", task.size))
        task.speed = int(torrent.get("dlspeed", 0))
        task.eta = int(torrent.get("eta", 0))
        state = torrent.get("state", "")
        if state in FINISHED_STATES or task.progress >= 1:
            content_path = Path(torrent["content_path"])
            final_files = await qb.files(task.torrent_hash)
            await qb.delete(task.torrent_hash, False)
            _clean_skipped_files(task, torrent, final_files)
            LOGGER.info("Task %s: torrent download complete", task.short_id())
            return content_path
        if state in ERROR_STATES:
            raise RuntimeError(f"qBittorrent entered state: {state}")
        await asyncio.sleep(2)
