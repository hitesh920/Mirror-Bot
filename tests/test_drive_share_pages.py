import asyncio

import pytest
from aiohttp.test_utils import make_mocked_request

from mirrorbot.services.drive_share_pages import DriveSharePages, render_share_page
from mirrorbot.services.drive_sharing import DriveShareFile, DriveShareManifest


@pytest.fixture
def manifest():
    return DriveShareManifest(
        "Public Folder",
        True,
        (
            DriveShareFile("Movie.mkv", "Public Folder/Movies", "https://example.com/movie"),
            DriveShareFile("Notes.txt", "Public Folder", "https://example.com/notes"),
        ),
        2,
    )


def test_share_page_has_names_links_search_and_clipboard_format(manifest):
    page = render_share_page(manifest)

    assert "Copy All Files and Links" in page
    assert 'id="search"' in page
    assert "Relative path" not in page
    assert "Public Folder/Movies/Movie.mkv" not in page
    assert "Movie.mkv" in page
    assert "Movie.mkv\\nhttps://example.com/movie\\n\\nNotes.txt\\nhttps://example.com/notes" in page


@pytest.mark.asyncio
async def test_create_and_close_share_server(monkeypatch, manifest):
    pages = DriveSharePages("http://127.0.0.1:8000", port=18004, timeout=300)
    started = stopped = False

    async def start():
        nonlocal started
        started = True

    async def stop():
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(pages, "_start_server", start)
    monkeypatch.setattr(pages, "_stop_server", stop)

    url = await pages.create(manifest)
    assert started
    assert url.startswith("http://127.0.0.1:18004/share/")
    assert len(pages.pages) == 1

    await pages.close_all()
    assert stopped
    assert not pages.pages


@pytest.mark.asyncio
async def test_invalid_token_is_not_found(manifest):
    pages = DriveSharePages("http://127.0.0.1:8000")
    request = make_mocked_request("GET", "/share/missing", match_info={"token": "missing"})

    with pytest.raises(Exception) as exc:
        await pages._show(request)
    assert getattr(exc.value, "status", None) == 404


@pytest.mark.asyncio
async def test_final_share_expiry_stops_server(monkeypatch, manifest):
    pages = DriveSharePages("http://127.0.0.1:8000", port=18004, timeout=0.01)
    stopped = asyncio.Event()

    async def start():
        return None

    async def stop():
        stopped.set()

    monkeypatch.setattr(pages, "_start_server", start)
    monkeypatch.setattr(pages, "_stop_server", stop)

    await pages.create(manifest)
    await asyncio.wait_for(stopped.wait(), timeout=1)

    assert not pages.pages
