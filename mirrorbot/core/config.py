from dataclasses import dataclass
from math import isfinite
from os import getenv
from pathlib import Path

from dotenv import load_dotenv


def _int(name: str, default: int = 0) -> int:
    value = getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _float(name: str, default: float, *, minimum: float = 0.0) -> float:
    value = getenv(name)
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if not isfinite(parsed):
        raise RuntimeError(f"{name} must be a finite number")
    if parsed < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return parsed


def _bounded_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = _int(name, default)
    if value < minimum or (maximum is not None and value > maximum):
        expected = (
            f"between {minimum} and {maximum}"
            if maximum is not None
            else f"at least {minimum}"
        )
        raise RuntimeError(f"{name} must be {expected}")
    return value


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
    r2_auto_delete_seconds: int
    cloudflare_account_id: str
    cloudflare_api_token: str
    download_dir: Path = Path("/app/downloads")
    qb_host: str = "http://localhost:8080"
    telegram_leech_split_size: int = 2_000_000_000
    ytdlp_max_video_quality: int = 1080
    ytdlp_audio_format: str = "mp3"
    ytdlp_audio_quality: str = "320"
    zip_compression_level: int = 5
    log_file: str = "logs/bot.log"
    disk_min_reserve_bytes: int = 5 * 1024**3
    disk_reserve_ratio: float = 0.05
    stall_timeout_seconds: int = 600
    guard_check_interval_seconds: int = 5
    torrent_metadata_timeout: int = 300
    torrent_add_timeout: int = 60

    @classmethod
    def load(cls) -> "Config":
        load_dotenv()
        required = [
            "BOT_TOKEN",
            "OWNER_ID",
            "TELEGRAM_API_ID",
            "TELEGRAM_API_HASH",
        ]
        missing = [key for key in required if not getenv(key)]
        if missing:
            raise RuntimeError(f"Missing required config: {', '.join(missing)}")

        return cls(
            bot_token=getenv("BOT_TOKEN", ""),
            owner_id=_int("OWNER_ID"),
            telegram_api_id=_int("TELEGRAM_API_ID"),
            telegram_api_hash=getenv("TELEGRAM_API_HASH", ""),
            task_limit=_bounded_int("TASK_LIMIT", 10, minimum=1),
            status_update_interval=_bounded_int(
                "STATUS_UPDATE_INTERVAL",
                10,
                minimum=1,
            ),
            public_base_url=getenv("PUBLIC_BASE_URL", ""),
            torrent_selection_port=_bounded_int(
                "TORRENT_SELECTION_PORT",
                8001,
                minimum=1,
                maximum=65535,
            ),
            torrent_selection_timeout=_bounded_int(
                "TORRENT_SELECTION_TIMEOUT",
                300,
                minimum=1,
            ),
            telegram_dump_chat_id=getenv("TELEGRAM_DUMP_CHAT_ID", "").strip(),
            r2_endpoint_url=getenv("R2_ENDPOINT_URL", "").strip().rstrip("/"),
            r2_bucket=getenv("R2_BUCKET", "").strip(),
            r2_access_key_id=getenv("R2_ACCESS_KEY_ID", "").strip(),
            r2_secret_access_key=getenv("R2_SECRET_ACCESS_KEY", "").strip(),
            r2_prefix=getenv("R2_PREFIX", "uploads/").strip(),
            r2_auto_delete_seconds=_bounded_int(
                "R2_AUTO_DELETE_SECONDS",
                172800,
                minimum=0,
            ),
            cloudflare_account_id=getenv(
                "CLOUDFLARE_ACCOUNT_ID",
                "",
            ).strip(),
            cloudflare_api_token=getenv(
                "CLOUDFLARE_API_TOKEN",
                "",
            ).strip(),
            log_file=getenv("LOG_FILE", "logs/bot.log").strip() or "logs/bot.log",
            disk_min_reserve_bytes=_bounded_int(
                "DISK_MIN_RESERVE_BYTES", 5 * 1024**3, minimum=0
            ),
            disk_reserve_ratio=_float("DISK_RESERVE_RATIO", 0.05, minimum=0.0),
            stall_timeout_seconds=_bounded_int(
                "STALL_TIMEOUT_SECONDS", 600, minimum=30
            ),
            guard_check_interval_seconds=_bounded_int(
                "GUARD_CHECK_INTERVAL_SECONDS", 5, minimum=1
            ),
            torrent_metadata_timeout=_bounded_int(
                "TORRENT_METADATA_TIMEOUT", 300, minimum=1
            ),
            torrent_add_timeout=_bounded_int("TORRENT_ADD_TIMEOUT", 60, minimum=1),
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

    @property
    def cloudflare_analytics_configured(self) -> bool:
        return bool(
            self.cloudflare_account_id and self.cloudflare_api_token and self.r2_bucket
        )
