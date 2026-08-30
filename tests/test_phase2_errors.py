"""Phase 2: unified error hierarchy and lossless error surfacing."""

import asyncio
from types import SimpleNamespace

import pytest

from mirrorbot.core.errors import TaskFailure
from mirrorbot.core.models import Source, SourceType
from mirrorbot.resolvers import base as resolver_base
from mirrorbot.resolvers import resolve_source
from mirrorbot.services import telegram_delivery
from mirrorbot.services.archive import (
    ArchiveCorruptError,
    ArchivePasswordError,
    ArchiveUnsupportedError,
)

_REAL_SLEEP = asyncio.sleep


def _fake_task():
    return SimpleNamespace(
        size=0, cancelled=False, current_file="", short_id=lambda: "abc123"
    )


# --- #7: archive errors are part of the TaskFailure hierarchy ---------------


@pytest.mark.parametrize(
    "error",
    [ArchiveCorruptError, ArchivePasswordError, ArchiveUnsupportedError],
)
def test_archive_errors_are_task_failures_with_processing_category(error):
    exc = error("boom")
    assert isinstance(exc, TaskFailure)
    assert exc.category == "processing"


# --- #8: resolver failures keep the real cause -----------------------------


class BoomResolver:
    name = "boom"

    def supports(self, url: str) -> bool:
        return "boom.test" in url

    async def resolve(self, url, session):
        raise ValueError("no download node in page")


async def test_resolver_failure_surfaces_cause_summary_and_chains(monkeypatch):
    import mirrorbot.resolvers as resolvers_pkg

    monkeypatch.setattr(resolvers_pkg, "RESOLVERS", (BoomResolver(),))

    with pytest.raises(resolver_base.ResolverError) as excinfo:
        await resolve_source(Source(SourceType.DIRECT_URL, "https://boom.test/x"))

    assert "unexpected format" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ValueError)


# --- N12: FloodWait handler passes args through and re-raises other errors --


async def test_floodwait_handler_retries_then_succeeds(monkeypatch):
    from pyrogram.errors import FloodWait

    monkeypatch.setattr(telegram_delivery, "TELEGRAM_FLOODWAIT_RETRIES", 3)
    monkeypatch.setattr(telegram_delivery.asyncio, "sleep", _fast_sleep)

    seen = []

    def _floodwait():
        exc = FloodWait("flood")
        exc.value = 1
        return exc

    async def fake_send(*args):
        seen.append(args)
        if len(seen) < 2:
            raise _floodwait()
        return "message"

    monkeypatch.setattr(telegram_delivery, "send_telegram_file", fake_send)

    task = _fake_task()
    result = await telegram_delivery.send_telegram_file_handling_floodwait(
        "client", task, 42, "item", "caption", "progress", "document", "meta", None
    )

    assert result == "message"
    assert len(seen) == 2
    # args forwarded verbatim, no duplicate task kwarg
    assert seen[0] == (
        "client",
        task,
        42,
        "item",
        "caption",
        "progress",
        "document",
        "meta",
        None,
    )


async def test_floodwait_handler_propagates_other_rpc_errors(monkeypatch):
    from pyrogram.errors import RPCError

    monkeypatch.setattr(telegram_delivery.asyncio, "sleep", _fast_sleep)

    async def fake_send(*_args):
        raise RPCError("nope")

    monkeypatch.setattr(telegram_delivery, "send_telegram_file", fake_send)
    task = _fake_task()

    with pytest.raises(RPCError):
        await telegram_delivery.send_telegram_file_handling_floodwait(
            "client", task, 42, "item", "caption", "progress", "document", "meta", None
        )


async def _fast_sleep(_seconds):
    await _REAL_SLEEP(0)
