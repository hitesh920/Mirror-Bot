import asyncio
import logging
from asyncio import to_thread
from pathlib import Path

from yt_dlp import YoutubeDL

from ..core.models import Task
from ..resolvers.base import safe_name

LOGGER = logging.getLogger(__name__)


class YtDlpLogger:
    def debug(self, _message):
        pass

    def info(self, message):
        LOGGER.info("yt-dlp: %s", message)

    def warning(self, message):
        LOGGER.warning("yt-dlp: %s", message)

    def error(self, message):
        LOGGER.error("yt-dlp: %s", message)


def _format_for(task: Task) -> dict:
    if task.options.ytdlp_kind == "audio":
        quality = task.options.ytdlp_quality or "320"
        return {
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": quality,
                }
            ],
        }
    quality = task.options.ytdlp_quality or "1080"
    return {"format": f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]"}


def _progress_hook(task: Task):
    def update(data):
        if task.cancelled:
            raise asyncio.CancelledError()
        if data.get("status") != "downloading":
            return
        task.set_transfer_stats(
            downloaded=int(data.get("downloaded_bytes") or 0),
            size=int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0),
            speed=int(data.get("speed") or 0),
            eta=int(data.get("eta") or 0),
        )

    return update


def output_template(task: Task) -> Path:
    if task.options.name:
        requested_name = safe_name(task.options.name, "yt-dlp")
        return task.work_dir / f"{requested_name}.%(ext)s"
    return task.work_dir / "%(title).180B.%(ext)s"


def select_download_result(work_dir: Path, before: set[Path]) -> Path:
    created = sorted(
        (path for path in work_dir.iterdir() if path not in before),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not created:
        raise RuntimeError("yt-dlp did not create an output file")
    return created[0] if len(created) == 1 else work_dir


async def download_ytdlp(task: Task) -> Path:
    task.work_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info(
        "Task %s: starting yt-dlp kind=%s quality=%s",
        task.short_id(),
        task.options.ytdlp_kind or "video",
        task.options.ytdlp_quality or "1080",
    )
    options = {
        "outtmpl": str(output_template(task)),
        "merge_output_format": "mp4",
        "noplaylist": False,
        "quiet": True,
        "no_warnings": True,
        "logger": YtDlpLogger(),
        "progress_hooks": [_progress_hook(task)],
        **_format_for(task),
    }

    def run() -> Path:
        before = set(task.work_dir.iterdir())
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(task.source.value, download=True)
            task.name = (
                info.get("title") or safe_name(task.options.name, "yt-dlp") or "yt-dlp"
            )
        return select_download_result(task.work_dir, before)

    result = await to_thread(run)
    if task.cancelled:
        raise asyncio.CancelledError()
    LOGGER.info("Task %s: yt-dlp download complete path=%s", task.short_id(), result)
    return result
