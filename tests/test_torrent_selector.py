"""Coverage for TorrentSelector's HTTP handlers (#30)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from mirrorbot.downloaders.torrent_selector import Selection, TorrentSelector


def _selector():
    qb = SimpleNamespace(
        set_file_priority=AsyncMock(),
        start=AsyncMock(),
    )
    return TorrentSelector(qb, "http://host:8001", 8001, timeout=300)


def _selection(files):
    return Selection(
        token="tok-123",
        torrent_hash="abcdef",
        files=files,
        submitted=asyncio.Event(),
        closed=asyncio.Event(),
    )


class FakeForm(dict):
    def getall(self, key, default=None):
        value = self.get(key)
        if value is None:
            return default if default is not None else []
        return value if isinstance(value, list) else [value]


def _request(token, form=None):
    return SimpleNamespace(
        match_info={"token": token},
        post=AsyncMock(return_value=FakeForm(form or {})),
    )


FILES = [
    {"index": 0, "name": "movie/video.mkv", "size": 100},
    {"index": 1, "name": "movie/subs/en.srt", "size": 5},
    {"index": 2, "name": "movie/sample.mkv", "size": 20},
]


async def test_submit_saves_selection_and_sets_priorities():
    selector = _selector()
    selection = _selection(FILES)
    selector.selections[selection.token] = selection

    response = await selector._submit(_request(selection.token, {"file": ["0", "2"]}))

    assert "saved" in response.text.lower()
    assert selection.submitted.is_set()
    selector.qb.set_file_priority.assert_any_await("abcdef", [1], 0)
    selector.qb.set_file_priority.assert_any_await("abcdef", [0, 2], 1)
    selector.qb.start.assert_awaited_once_with("abcdef")


async def test_submit_cancel_action_marks_selection_cancelled():
    selector = _selector()
    selection = _selection(FILES)
    selector.selections[selection.token] = selection

    response = await selector._submit(
        _request(selection.token, {"action": "cancel"})
    )

    assert selection.cancelled is True
    assert selection.submitted.is_set()
    assert "cancel" in response.text.lower()
    selector.qb.start.assert_not_awaited()


async def test_submit_requires_at_least_one_valid_file():
    selector = _selector()
    selection = _selection(FILES)
    selector.selections[selection.token] = selection

    response = await selector._submit(
        _request(selection.token, {"file": ["99"]})
    )

    assert response.status == 400
    assert not selection.submitted.is_set()
    selector.qb.start.assert_not_awaited()


async def test_show_renders_every_file_label():
    selector = _selector()
    selection = _selection(FILES)
    selector.selections[selection.token] = selection

    response = await selector._show(_request(selection.token))

    assert "video.mkv" in response.text
    assert "en.srt" in response.text
    assert "value='0'" in response.text


async def test_unknown_token_is_not_found():
    selector = _selector()
    selector.selections["tok-123"] = _selection(FILES)

    with pytest.raises(web.HTTPNotFound):
        selector._selection(_request("wrong-token"))


async def test_get_returns_none_for_unknown_hash():
    selector = _selector()
    assert await selector.get("nope") is None
