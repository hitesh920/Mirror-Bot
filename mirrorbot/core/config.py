from dataclasses import dataclass
from os import getenv
from pathlib import Path

from dotenv import load_dotenv


def _int(name: str, default: int = 0) -> int:
    value = getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _bool(name: str, default: bool = False) -> bool:
    value = getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    telegram_api_id: int
    telegram_api_hash: str
    task_limit: int
    status_update_interval: int
    public_base_url: str
    torrent_selection_port: int
    torrent_selection_timeout: int
    telegram_dump_chat_id: str
    r2_endpoint_url: str
    r2_bucket: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_prefix: str
    r2_link_expiry_seconds: int
    r2_auto_delete_seconds: int
    enable_telegram_ui: bool

    download_dir: Path = Path("/app/downloads")
    qb_host: str = "http://localhost:8080"
    telegram_leech_split_size: int = 2_000_000_000
    ytdlp_max_video_quality: int = 1080
    ytdlp_audio_format: str = "mp3"
    ytdlp_audio_quality: str = "320"
    zip_compression_level: int = 5
    log_file: str = "logs/bot.log"

    @classmethod
    def load(cls) -> "Config":
        load_dotenv()
        enable_telegram_ui = _bool("ENABLE_TELEGRAM_UI", True)
        required = []
        if enable_telegram_ui:
            required.extend([
                "BOT_TOKEN",
                "OWNER_ID",
                "TELEGRAM_API_ID",
                "TELEGRAM_API_HASH",
            ])
        missing = [key for key in required if not getenv(key)]
        if missing:
            raise RuntimeError(f"Missing required config: {', '.join(missing)}")

        return cls(
            bot_token=getenv("BOT_TOKEN", ""),
            owner_id=_int("OWNER_ID"),
            telegram_api_id=_int("TELEGRAM_API_ID"),
            telegram_api_hash=getenv("TELEGRAM_API_HASH", ""),
            task_limit=max(1, _int("TASK_LIMIT", 10)),
            status_update_interval=max(1, _int("STATUS_UPDATE_INTERVAL", 10)),
            public_base_url=getenv("PUBLIC_BASE_URL", ""),
            torrent_selection_port=_int("TORRENT_SELECTION_PORT", 8001),
            torrent_selection_timeout=_int("TORRENT_SELECTION_TIMEOUT", 300),
            telegram_dump_chat_id=getenv("TELEGRAM_DUMP_CHAT_ID", "").strip(),
            r2_endpoint_url=getenv("R2_ENDPOINT_URL", "").strip().rstrip("/"),
            r2_bucket=getenv("R2_BUCKET", "").strip(),
            r2_access_key_id=getenv("R2_ACCESS_KEY_ID", "").strip(),
            r2_secret_access_key=getenv("R2_SECRET_ACCESS_KEY", "").strip(),
            r2_prefix=getenv("R2_PREFIX", "uploads/").strip(),
            r2_link_expiry_seconds=max(
                1,
                min(604800, _int("R2_LINK_EXPIRY_SECONDS", 86400)),
            ),
            r2_auto_delete_seconds=max(
                0,
                _int("R2_AUTO_DELETE_SECONDS", 172800),
            ),
            enable_telegram_ui=enable_telegram_ui,
        )

    @property
    def r2_configured(self) -> bool:
        return all(
            (
                self.r2_endpoint_url,
                self.r2_bucket,
                self.r2_access_key_id,
                self.r2_secret_access_key,
            )
        )
