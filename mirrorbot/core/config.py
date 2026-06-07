from dataclasses import dataclass
from os import getenv
from pathlib import Path

from dotenv import load_dotenv


def _int(name: str, default: int = 0) -> int:
    value = getenv(name)
    if value is None or value == "":
        return default
    return int(value)


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    telegram_api_id: int
    telegram_api_hash: str
    local_download_root: Path
    google_drive_folder_id: str
    rclone_remote_path: str
    task_limit: int
    status_update_interval: int
    public_base_url: str
    torrent_selection_port: int
    torrent_selection_timeout: int

    download_dir: Path = Path("/app/downloads")
    qb_host: str = "http://localhost:8080"
    telegram_leech_split_size: int = 2_000_000_000
    ytdlp_max_video_quality: int = 1080
    ytdlp_audio_format: str = "mp3"
    ytdlp_audio_quality: str = "320"
    zip_compression_level: int = 5
    log_file: str = "logs/bot.log"
    rclone_config_file: Path = Path("/app/data/rclone/rclone.conf")

    @classmethod
    def load(cls) -> "Config":
        load_dotenv()
        missing = [
            key
            for key in [
                "BOT_TOKEN",
                "OWNER_ID",
                "TELEGRAM_API_ID",
                "TELEGRAM_API_HASH",
                "LOCAL_DOWNLOAD_ROOT",
            ]
            if not getenv(key)
        ]
        if missing:
            raise RuntimeError(f"Missing required config: {', '.join(missing)}")

        return cls(
            bot_token=getenv("BOT_TOKEN", ""),
            owner_id=_int("OWNER_ID"),
            telegram_api_id=_int("TELEGRAM_API_ID"),
            telegram_api_hash=getenv("TELEGRAM_API_HASH", ""),
            local_download_root=Path(getenv("LOCAL_DOWNLOAD_ROOT", "")),
            google_drive_folder_id=getenv("GOOGLE_DRIVE_FOLDER_ID", ""),
            rclone_remote_path=getenv("RCLONE_REMOTE_PATH", ""),
            task_limit=max(1, _int("TASK_LIMIT", 10)),
            status_update_interval=max(1, _int("STATUS_UPDATE_INTERVAL", 10)),
            public_base_url=getenv("PUBLIC_BASE_URL", "http://localhost:8000"),
            torrent_selection_port=_int("TORRENT_SELECTION_PORT", 8000),
            torrent_selection_timeout=_int("TORRENT_SELECTION_TIMEOUT", 300),
        )
