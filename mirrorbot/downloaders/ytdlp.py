from asyncio import to_thread
from pathlib import Path

from yt_dlp import YoutubeDL

from ..models import Task


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


async def download_ytdlp(task: Task) -> Path:
    task.work_dir.mkdir(parents=True, exist_ok=True)
    output = task.work_dir / "%(title).180B.%(ext)s"
    options = {
        "outtmpl": str(output),
        "merge_output_format": "mp4",
        "noplaylist": False,
        "quiet": True,
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

    return await to_thread(run)

