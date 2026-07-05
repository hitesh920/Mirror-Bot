import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)
THUMBNAIL_MAX_BYTES = 200_000


@dataclass(frozen=True)
class MediaMetadata:
    is_video: bool = False
    is_audio: bool = False
    duration: int = 0
    width: int = 0
    height: int = 0
    artist: str = ""
    title: str = ""


async def _run_command(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise
    return (
        process.returncode,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


async def probe_media(path: Path) -> MediaMetadata:
    try:
        code, stdout, stderr = await _run_command(
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        )
    except Exception:
        LOGGER.debug("Media probe failed path=%s", path, exc_info=True)
        return MediaMetadata()
    if code != 0 or not stdout:
        LOGGER.debug("Media probe returned code=%s path=%s stderr=%s", code, path, stderr[:300])
        return MediaMetadata()
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        LOGGER.debug("Media probe returned invalid JSON path=%s", path)
        return MediaMetadata()

    video_stream = None
    has_audio = False
    for stream in payload.get("streams") or []:
        codec_type = stream.get("codec_type")
        if codec_type == "video":
            codec = str(stream.get("codec_name") or "").lower()
            if codec not in {"mjpeg", "png", "bmp"} and video_stream is None:
                video_stream = stream
        elif codec_type == "audio":
            has_audio = True

    format_info = payload.get("format") or {}
    duration = _duration(format_info.get("duration"))
    if duration <= 0 and video_stream:
        duration = _duration(video_stream.get("duration"))
    tags = format_info.get("tags") or {}
    return MediaMetadata(
        is_video=video_stream is not None,
        is_audio=has_audio,
        duration=duration,
        width=int((video_stream or {}).get("width") or 0),
        height=int((video_stream or {}).get("height") or 0),
        artist=str(tags.get("artist") or tags.get("ARTIST") or tags.get("Artist") or ""),
        title=str(tags.get("title") or tags.get("TITLE") or tags.get("Title") or ""),
    )


async def create_video_thumbnail(video: Path, output_dir: Path, duration: int) -> Path | None:
    output_dir.mkdir(parents=True, exist_ok=True)
    seek = max(1, (duration or 6) // 2)
    for quality in (4, 7, 10):
        output = output_dir / f"{video.stem[:80]}.q{quality}.jpg"
        try:
            code, _stdout, stderr = await _run_command(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                str(seek),
                "-i",
                str(video),
                "-vf",
                "thumbnail,scale='min(320,iw)':-2",
                "-frames:v",
                "1",
                "-q:v",
                str(quality),
                "-y",
                str(output),
                timeout=90,
            )
        except Exception:
            LOGGER.debug("Video thumbnail generation failed path=%s", video, exc_info=True)
            return None
        if code != 0 or not output.exists():
            LOGGER.debug("Video thumbnail command failed path=%s stderr=%s", video, stderr[:300])
            return None
        if output.stat().st_size <= THUMBNAIL_MAX_BYTES or quality == 10:
            return output
        output.unlink(missing_ok=True)
    return None


def _duration(value) -> int:
    try:
        return max(0, round(float(value or 0)))
    except (TypeError, ValueError):
        return 0
