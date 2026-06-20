from pathlib import Path
from uuid import uuid4

import pytest

from mirrorbot.core.models import AddOptions, Destination, Source, SourceType, Task, TaskPhase
from mirrorbot.core.parser import parse_add_text
from mirrorbot.core.source_detector import detect_source
from mirrorbot.services.web.auth import credentials_match, is_public_path
from mirrorbot.services.web_dashboard import WebDashboard
from mirrorbot.telegram.state import ExpiringStore


def make_task() -> Task:
    return Task(
        id=str(uuid4()),
        user_id=1,
        chat_id=1,
        message_id=1,
        source=Source(SourceType.DIRECT_URL, "https://example.com/file.bin"),
        destination=Destination.TELEGRAM,
        options=AddOptions(),
        work_dir=Path("unused"),
    )


def test_parse_add_text_flags():
    link, options = parse_add_text("/add https://example.com/a.zip -zp secret -e -n custom")
    assert link == "https://example.com/a.zip"
    assert options.zip is True
    assert options.zip_password == "secret"
    assert options.extract is True
    assert options.name == "custom"


def test_detect_source_common_inputs():
    assert detect_source("magnet:?xt=urn:btih:abcd").type == SourceType.MAGNET
    assert detect_source("https://drive.google.com/file/d/abc/view").type == SourceType.GOOGLE_DRIVE
    assert detect_source("https://example.com/file.bin").type == SourceType.DIRECT_URL


def test_task_cancel_is_idempotent():
    task = make_task()
    assert task.request_cancel("test") is True
    assert task.request_cancel("again") is False
    assert task.cancelled is True
    assert task.cancel_event.is_set()
    assert task.cancel_reason == "test"


def test_terminal_transition_is_not_overwritten():
    task = make_task()
    task.transition(TaskPhase.COMPLETE)
    task.transition(TaskPhase.ERROR)
    assert task.phase == TaskPhase.COMPLETE


def test_expiring_store_take_and_expiry():
    store = ExpiringStore[str](ttl_seconds=30)
    store.put("token", "value")
    assert store.get("token") == "value"
    assert store.take("token") == "value"
    assert store.take("token") is None

    expired = ExpiringStore[str](ttl_seconds=-1)
    expired.put("old", "value")
    assert expired.get("old") is None


def test_web_auth_helpers():
    assert is_public_path("/login")
    assert is_public_path("/assets/index.js")
    assert not is_public_path("/api/state")
    assert credentials_match("owner", "secret", "owner", "secret")
    assert not credentials_match("owner", "secret", "owner", "wrong")


def test_web_destination_validation_accepts_aliases():
    dashboard = WebDashboard.__new__(WebDashboard)
    assert dashboard.destination_from_form("local", "series") == Destination.LOCAL_SERIES
    assert dashboard.destination_from_form("gdrive") == Destination.GOOGLE_DRIVE
    assert dashboard.destination_from_form("google_drive") == Destination.GOOGLE_DRIVE

    with pytest.raises(Exception):
        dashboard.destination_from_form("")
