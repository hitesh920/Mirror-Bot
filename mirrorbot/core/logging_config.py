import atexit
import json
import logging
import logging.handlers
import queue
import re
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


APP_LOG_MAX_BYTES = 5 * 1024 * 1024
APP_LOG_TOTAL_BYTES = 50 * 1024 * 1024
APP_LOG_RETENTION_SECONDS = 7 * 24 * 60 * 60
APP_LOG_BACKUPS = 20
EXPORT_LINE_LIMIT = 2_000
MASK = "[REDACTED]"

NOISY_LOGGERS = {
    "pyrogram": logging.ERROR,
    "aiohttp": logging.WARNING,
    "asyncio": logging.WARNING,
    "urllib3": logging.WARNING,
    "requests": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
}

SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "bot_token",
    "cookie",
    "credentials",
    "key",
    "password",
    "refresh_token",
    "resourcekey",
    "secret",
    "signature",
    "token",
}
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(" + "|".join(re.escape(key) for key in sorted(SENSITIVE_KEYS)) + r")"
    r"(\s*[:=]\s*)([^\s,;&]+)"
)
BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
BOT_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
MAGNET = re.compile(r"(?i)magnet:\?[^\s<>'\"]+")
TEMP_PAGE_URL = re.compile(
    r"(?i)(https?://[^\s<>'\"]+/(?:select|search|share|local)/)[A-Za-z0-9_-]+"
)
URL = re.compile(r"https?://[^\s<>'\"]+")

_listener: logging.handlers.QueueListener | None = None


def _sanitize_url(match: re.Match) -> str:
    raw = match.group(0)
    try:
        parts = urlsplit(raw)
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            query.append((key, MASK if key.lower() in SENSITIVE_KEYS else value))
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
    except ValueError:
        return raw


def sanitize_text(value: object) -> str:
    text = str(value)
    text = BOT_TOKEN.sub(MASK, text)
    text = BEARER.sub(lambda match: f"{match.group(1)} {MASK}", text)
    text = MAGNET.sub("magnet:[REDACTED]", text)
    text = TEMP_PAGE_URL.sub(lambda match: f"{match.group(1)}{MASK}", text)
    text = SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{MASK}", text
    )
    return URL.sub(_sanitize_url, text)


def sanitize_log_file(path: Path) -> None:
    if not path.is_file():
        return
    original = path.read_text(encoding="utf-8", errors="replace")
    sanitized = sanitize_text(original)
    if sanitized != original:
        path.write_text(sanitized, encoding="utf-8")


def _field_value(value: object) -> str:
    sanitized = sanitize_text(value)
    if not sanitized or any(char.isspace() for char in sanitized):
        return json.dumps(sanitized, ensure_ascii=True)
    return sanitized


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: object,
) -> None:
    values = [f"event={_field_value(event)}"]
    values.extend(
        f"{key}={_field_value(value)}"
        for key, value in fields.items()
        if value not in (None, "")
    )
    logger.log(level, " ".join(values))


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        clone = logging.makeLogRecord(record.__dict__.copy())
        clone.msg = sanitize_text(record.getMessage())
        clone.args = ()
        return sanitize_text(super().format(clone))

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")


class RetentionRotatingFileHandler(logging.handlers.RotatingFileHandler):
    def __init__(
        self,
        filename: Path,
        *,
        max_bytes: int,
        total_bytes: int,
        retention_seconds: int,
        backup_count: int,
    ):
        self.total_bytes = total_bytes
        self.retention_seconds = retention_seconds
        super().__init__(
            filename,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        self.prune()

    def doRollover(self) -> None:
        super().doRollover()
        self.prune()

    def prune(self) -> None:
        base = Path(self.baseFilename)
        files = sorted(
            (
                path
                for path in base.parent.glob(f"{base.name}*")
                if path.is_file()
            ),
            key=lambda path: path.stat().st_mtime,
        )
        cutoff = time() - self.retention_seconds
        for path in list(files):
            if path == base:
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                files.remove(path)
        total = sum(path.stat().st_size for path in files if path.exists())
        for path in files:
            if total <= self.total_bytes:
                break
            if path == base:
                continue
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size


def _stop_listener() -> None:
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None


def setup_logging(log_file: str = "logs/bot.log") -> None:
    global _listener
    _stop_listener()
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for existing in log_files(log_path):
        sanitize_log_file(existing)

    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)

    rotating_file = RetentionRotatingFileHandler(
        log_path,
        max_bytes=APP_LOG_MAX_BYTES,
        total_bytes=APP_LOG_TOTAL_BYTES,
        retention_seconds=APP_LOG_RETENTION_SECONDS,
        backup_count=APP_LOG_BACKUPS,
    )
    rotating_file.setLevel(logging.INFO)
    rotating_file.setFormatter(formatter)

    records: queue.SimpleQueue = queue.SimpleQueue()
    queued = logging.handlers.QueueHandler(records)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.WARNING)
    root.addHandler(queued)

    logging.getLogger("mirrorbot").setLevel(logging.INFO)
    for name, level in NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(level)

    _listener = logging.handlers.QueueListener(
        records, console, rotating_file, respect_handler_level=True
    )
    _listener.start()


def log_files(log_file: str | Path) -> list[Path]:
    base = Path(log_file)
    rotations = sorted(
        (
            path
            for path in base.parent.glob(f"{base.name}.*")
            if path.is_file() and path.name[len(base.name) + 1 :].isdigit()
        ),
        key=lambda path: path.stat().st_mtime,
    )
    return rotations + ([base] if base.is_file() else [])


def create_log_export(log_file: str | Path, line_limit: int = EXPORT_LINE_LIMIT) -> Path | None:
    lines: list[str] = []
    for path in log_files(log_file):
        lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
    if not lines:
        return None
    output_dir = Path(log_file).parent
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=".mirror-bot-logs-",
        suffix=".txt",
        dir=output_dir,
        delete=False,
    ) as exported:
        exported.write("\n".join(sanitize_text(line) for line in lines[-line_limit:]))
        exported.write("\n")
        return Path(exported.name)


atexit.register(_stop_listener)
