"""Phase 5a: tunables moved into Config and wired to the engines."""

from types import SimpleNamespace

import pytest

from mirrorbot.core.config import Config
from mirrorbot.downloaders import torrent as torrent_engine
from mirrorbot.services import transfer_guard


@pytest.fixture
def _env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("OWNER_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "h")


def test_config_defaults_for_new_tunables(_env):
    config = Config.load()
    assert config.disk_min_reserve_bytes == 5 * 1024**3
    assert config.disk_reserve_ratio == 0.05
    assert config.stall_timeout_seconds == 600
    assert config.guard_check_interval_seconds == 5
    assert config.torrent_metadata_timeout == 300
    assert config.torrent_add_timeout == 60
    assert config.log_file == "logs/bot.log"


def test_config_reads_tunables_from_env(_env, monkeypatch):
    monkeypatch.setenv("DISK_MIN_RESERVE_BYTES", "1073741824")
    monkeypatch.setenv("DISK_RESERVE_RATIO", "0.1")
    monkeypatch.setenv("STALL_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("TORRENT_METADATA_TIMEOUT", "90")
    monkeypatch.setenv("LOG_FILE", "/var/log/mirror.log")

    config = Config.load()

    assert config.disk_min_reserve_bytes == 1073741824
    assert config.disk_reserve_ratio == 0.1
    assert config.stall_timeout_seconds == 120
    assert config.torrent_metadata_timeout == 90
    assert config.log_file == "/var/log/mirror.log"


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("DISK_RESERVE_RATIO", "abc", "must be a number"),
        ("DISK_RESERVE_RATIO", "-0.1", "at least"),
        ("STALL_TIMEOUT_SECONDS", "5", "at least 30"),
        ("TORRENT_ADD_TIMEOUT", "0", "at least 1"),
    ],
)
def test_config_rejects_bad_tunables(_env, monkeypatch, name, value, message):
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=message):
        Config.load()


def test_configure_applies_config_to_engine_modules(monkeypatch):
    monkeypatch.setattr(transfer_guard, "MIN_RESERVE", 0)
    monkeypatch.setattr(transfer_guard, "RESERVE_RATIO", 0.0)
    monkeypatch.setattr(transfer_guard, "STALL_TIMEOUT", 0)
    monkeypatch.setattr(transfer_guard, "CHECK_INTERVAL", 0)
    monkeypatch.setattr(torrent_engine, "TORRENT_METADATA_TIMEOUT", 0)
    monkeypatch.setattr(torrent_engine, "TORRENT_ADD_ATTEMPTS", 0)

    config = SimpleNamespace(
        disk_min_reserve_bytes=999,
        disk_reserve_ratio=0.2,
        stall_timeout_seconds=42,
        guard_check_interval_seconds=3,
        torrent_metadata_timeout=7,
        torrent_add_timeout=9,
    )
    transfer_guard.configure(config)
    torrent_engine.configure(config)

    assert transfer_guard.MIN_RESERVE == 999
    assert transfer_guard.RESERVE_RATIO == 0.2
    assert transfer_guard.STALL_TIMEOUT == 42
    assert transfer_guard.CHECK_INTERVAL == 3
    assert torrent_engine.TORRENT_METADATA_TIMEOUT == 7
    assert torrent_engine.TORRENT_ADD_ATTEMPTS == 9
