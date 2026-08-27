"""Coverage for the PendingAdds store extracted from app.py (#19)."""

import asyncio

from mirrorbot.core.models import AddOptions, Source, SourceType
from mirrorbot.telegram import pending as pending_module
from mirrorbot.telegram.pending import PendingAdd, PendingAdds


class FakeBackground:
    def __init__(self):
        self.jobs = []

    def create(self, coro, name=""):
        job = asyncio.ensure_future(coro)
        self.jobs.append(job)
        return job


class FakeMessage:
    def __init__(self):
        self.edited = None

    async def edit(self, text):
        self.edited = text


def _pending():
    return PendingAdd(
        sources=[Source(SourceType.DIRECT_URL, "https://example.com/a")],
        options=AddOptions(),
    )


def test_setitem_get_and_contains():
    store = PendingAdds(FakeBackground())
    store["7"] = _pending()

    assert "7" in store
    assert store.get("7") is not None
    assert store.get("missing") is None


def test_take_missing_returns_none():
    assert PendingAdds(FakeBackground()).take("nope") is None


async def test_take_removes_and_cancels_expiry(monkeypatch):
    monkeypatch.setattr(pending_module, "PENDING_ADD_TIMEOUT", 3600)
    background = FakeBackground()
    store = PendingAdds(background)
    store["7"] = _pending()
    store.track_expiry("7", FakeMessage())
    await asyncio.sleep(0)  # let the expiry job park in its sleep

    taken = store.take("7")
    await asyncio.sleep(0)

    assert taken is not None
    assert "7" not in store
    assert background.jobs[0].cancelled()


async def test_expiry_edits_message_and_clears(monkeypatch):
    monkeypatch.setattr(pending_module, "PENDING_ADD_TIMEOUT", 0)
    background = FakeBackground()
    store = PendingAdds(background)
    message = FakeMessage()
    store["7"] = _pending()
    store.track_expiry("7", message)
    await asyncio.gather(*background.jobs)

    assert message.edited == "Selection expired. Send /add again."
    assert "7" not in store


async def test_cancel_all_cancels_pending_jobs(monkeypatch):
    monkeypatch.setattr(pending_module, "PENDING_ADD_TIMEOUT", 3600)
    background = FakeBackground()
    store = PendingAdds(background)
    store["7"] = _pending()
    store.track_expiry("7", FakeMessage())
    await asyncio.sleep(0)

    store.cancel_all()
    await asyncio.sleep(0)

    assert background.jobs[0].cancelled()
