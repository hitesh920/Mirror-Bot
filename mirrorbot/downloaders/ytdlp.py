import asyncio
from asyncio import to_thread
import logging
from pathlib import Path

from yt_dlp import YoutubeDL

from ..core.models import Task

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
        return {
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
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
        task.downloaded = int(data.get("downloaded_bytes") or 0)
        task.size = int(
            data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        )
        task.speed = int(data.get("speed") or 0)
        task.eta = int(data.get("eta") or 0)
        task.progress = task.downloaded / task.size if task.size else 0

    return update


async def download_ytdlp(task: Task) -> Path:
    task.work_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info(
        "Task %s: starting yt-dlp kind=%s quality=%s",
        task.short_id(),
        task.options.ytdlp_kind or "video",
        task.options.ytdlp_quality or "1080",
    )
    output = task.work_dir / "%(title).180B.%(ext)s"
    options = {
        "outtmpl": str(output),
        "merge_output_format": "mp4",
        "noplaylist": False,
        "quiet": True,
        "no_warnings": True,
        "logger": YtDlpLogger(),
        "progress_hooks": [_progress_hook(task)],
        **_format_for(task),
    }
    if task.options.name:
        options["outtmpl"] = str(task.work_dir / f"{task.options.name}.%(ext)s")

    def run() -> Path:
        before = set(task.work_dir.iterdir())
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(task.source.value, download=True)
            task.name = info.get("title") or task.options.name or "yt-dlp"
        after = set(task.work_dir.iterdir())
        created = sorted(after - before, key=lambda p: p.stat().st_mtime, reverse=True)
        if created:
            return created[0]
        files = sorted(task.work_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            raise RuntimeError("yt-dlp did not create an output file")
        return files[0]

    result = await to_thread(run)
    if task.cancelled:
        raise asyncio.CancelledError()
    LOGGER.info("Task %s: yt-dlp download complete path=%s", task.short_id(), result)
    return result
