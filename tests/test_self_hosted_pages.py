import asyncio

import pytest
from aiohttp.test_utils import make_mocked_request

from mirrorbot.downloaders.torrent_selector import Selection, TorrentSelector
from mirrorbot.services.drive_search_pages import render_search_page
from mirrorbot.services.file_explorer import PAGE
from mirrorbot.services.google_drive_delivery import FOLDER_MIME_TYPE


def test_file_explorer_uses_parent_row_navigation():
    assert 'onclick="up()">..</td>' in PAGE
    assert '>Up</button>' not in PAGE
    assert "Local files" in PAGE
    assert "item${d.items.length===1?'':'s'}" in PAGE


def test_drive_search_page_has_filter_and_summary():
    page = render_search_page(
        "movie",
        [
            {
                "id": "folder",
                "name": "Movies",
                "mimeType": FOLDER_MIME_TYPE,
                "size": "1024",
            }
        ],
    )

    assert 'id="filter"' in page
    assert "1 results" in page
    assert "No matching results" in page
    assert "Movies" in page


@pytest.mark.asyncio
async def test_torrent_selector_page_has_search_and_selected_count():
    selector = TorrentSelector(None, "http://localhost:8000", 8000, 300)
    selector.selection = Selection(
        "token",
        "hash",
        [{"index": 0, "name": "Folder/Movie.mkv", "size": 1024}],
        asyncio.Event(),
        asyncio.Event(),
    )
    request = make_mocked_request(
        "GET",
        "/select/token",
        match_info={"token": "token"},
    )

    response = await selector._show(request)
    page = response.text

    assert 'id="search"' in page
    assert 'id="count">0 files selected' in page
    assert "Nothing is selected by default" in page
    assert "Start download" in page
    assert ".folder-name{padding:2px 0" in page
    assert "font-weight:650;border:0" in page
