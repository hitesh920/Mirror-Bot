import logging
import os
from pathlib import Path
from time import time

from mirrorbot.core.logging_config import (
    MASK,
    RedactingFormatter,
    RetentionRotatingFileHandler,
    create_log_export,
    log_event,
    sanitize_log_file,
    sanitize_text,
)
from mirrorbot.downloaders.qbittorrent import QBittorrentClient


def test_sanitize_text_masks_secrets_and_temporary_urls():
    text = (
        "token=secret BOT_TOKEN=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ "
        "Authorization: Bearer top.secret "
        "magnet:?xt=urn:btih:abc&tr=udp://tracker "
        "http://example.com/share/random-token "
        "https://example.com/file?resourcekey=private&name=public"
    )
    sanitized = sanitize_text(text)
    assert "secret" not in sanitized
    assert "top.secret" not in sanitized
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in sanitized
    assert "urn:btih" not in sanitized
    assert "/share/[REDACTED]" in sanitized
    assert "resourcekey=%5BREDACTED%5D" in sanitized
    assert "name=public" in sanitized


def test_redacting_formatter_sanitizes_exception_traceback():
    formatter = RedactingFormatter("%(levelname)s %(message)s")
    try:
        raise RuntimeError("password=very-secret")
    except RuntimeError:
        record = logging.LogRecord(
            "mirrorbot.test",
            logging.ERROR,
            __file__,
            1,
            "failed token=also-secret",
            (),
            exc_info=__import__("sys").exc_info(),
        )
    output = formatter.format(record)
    assert "very-secret" not in output
    assert "also-secret" not in output
    assert "RuntimeError" in output
    assert MASK in output


def test_log_event_uses_stable_fields(caplog):
    logger = logging.getLogger("mirrorbot.test.events")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_event(
            logger,
            logging.INFO,
            "task.completed",
            task="abc123",
            phase="complete",
            destination="local movies",
        )
    assert "event=task.completed" in caplog.text
    assert "task=abc123" in caplog.text
    assert 'destination="local movies"' in caplog.text


def test_log_export_uses_latest_lines_and_sanitizes_again(tmp_path: Path):
    current = tmp_path / "bot.log"
    rotated = tmp_path / "bot.log.1"
    rotated.write_text("old token=secret\nold safe\n", encoding="utf-8")
    current.write_text("new password=secret\nnew safe\n", encoding="utf-8")
    os.utime(rotated, (time() - 10, time() - 10))
    exported = create_log_export(current, line_limit=3)
    assert exported is not None
    try:
        lines = exported.read_text(encoding="utf-8").splitlines()
        assert lines == ["old safe", f"new password={MASK}", "new safe"]
    finally:
        exported.unlink(missing_ok=True)


def test_log_export_defaults_to_latest_2000_application_lines(tmp_path: Path):
    current = tmp_path / "bot.log"
    current.write_text(
        "\n".join(f"line-{index}" for index in range(2_005)),
        encoding="utf-8",
    )
    (tmp_path / "qbittorrent.log").write_text("engine-only", encoding="utf-8")
    exported = create_log_export(current)
    assert exported is not None
    try:
        lines = exported.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2_000
        assert lines[0] == "line-5"
        assert "engine-only" not in lines
    finally:
        exported.unlink(missing_ok=True)


def test_existing_log_is_sanitized_during_upgrade(tmp_path: Path):
    log = tmp_path / "bot.log"
    log.write_text(
        "url=http://example.com/share/legacy-token password=legacy-secret\n",
        encoding="utf-8",
    )
    sanitize_log_file(log)
    output = log.read_text(encoding="utf-8")
    assert "legacy-token" not in output
    assert "legacy-secret" not in output
    assert MASK in output


def test_retention_handler_prunes_age_and_total_size(tmp_path: Path):
    current = tmp_path / "bot.log"
    old = tmp_path / "bot.log.2"
    oldest = tmp_path / "bot.log.3"
    current.write_bytes(b"x" * 80)
    old.write_bytes(b"x" * 80)
    oldest.write_bytes(b"x" * 80)
    os.utime(oldest, (time() - 1000, time() - 1000))
    handler = RetentionRotatingFileHandler(
        current,
        max_bytes=100,
        total_bytes=100,
        retention_seconds=100,
        backup_count=5,
    )
    handler.close()
    assert not oldest.exists()
    assert not old.exists()
    assert current.exists()


def test_qbittorrent_reads_private_password_file(tmp_path: Path):
    password = tmp_path / "webui-password"
    password.write_text("temporary-password\n", encoding="utf-8")
    client = QBittorrentClient("http://localhost:8080", password)
    assert client._temporary_password() == "temporary-password"
