#!/usr/bin/env python3
import os
import re
import signal
import subprocess
from pathlib import Path
from time import time


LOG_DIR = Path("/app/logs")
LOG_FILE = LOG_DIR / "qbittorrent.log"
PASSWORD_FILE = Path("/app/data/qbittorrent/webui-password")
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
RETENTION_SECONDS = 7 * 24 * 60 * 60
PASSWORD_PATTERN = re.compile(
    r"(temporary password is provided for this session:\s*)(\S+)", re.IGNORECASE
)


def prune() -> None:
    files = sorted(
        (path for path in LOG_DIR.glob("qbittorrent.log*") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
    )
    cutoff = time() - RETENTION_SECONDS
    for path in list(files):
        if path != LOG_FILE and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            files.remove(path)
    total = sum(path.stat().st_size for path in files if path.exists())
    for path in files:
        if total <= MAX_TOTAL_BYTES:
            break
        if path == LOG_FILE:
            continue
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        total -= size


def sanitize_existing_logs() -> None:
    for path in LOG_DIR.glob("qbittorrent.log*"):
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8", errors="replace")
        sanitized = PASSWORD_PATTERN.sub(r"\1[REDACTED]", original)
        if sanitized != original:
            path.write_text(sanitized, encoding="utf-8")


def rotate() -> None:
    if not LOG_FILE.exists() or LOG_FILE.stat().st_size < MAX_FILE_BYTES:
        return
    for index in range(3, 0, -1):
        source = LOG_FILE.with_name(f"{LOG_FILE.name}.{index}")
        target = LOG_FILE.with_name(f"{LOG_FILE.name}.{index + 1}")
        if source.exists():
            source.replace(target)
    LOG_FILE.replace(LOG_FILE.with_name(f"{LOG_FILE.name}.1"))
    prune()


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    PASSWORD_FILE.unlink(missing_ok=True)
    sanitize_existing_logs()
    prune()
    process = subprocess.Popen(
        [
            "qbittorrent-nox",
            "--confirm-legal-notice",
            "--webui-port=8080",
            "--profile=/app/data/qbittorrent",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    def stop(_signum, _frame):
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    assert process.stdout is not None
    for line in process.stdout:
        match = PASSWORD_PATTERN.search(line)
        if match:
            PASSWORD_FILE.write_text(match.group(2), encoding="utf-8")
            os.chmod(PASSWORD_FILE, 0o600)
            line = PASSWORD_PATTERN.sub(r"\1[REDACTED]", line)
        rotate()
        with LOG_FILE.open("a", encoding="utf-8") as output:
            output.write(line)
    PASSWORD_FILE.unlink(missing_ok=True)
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
