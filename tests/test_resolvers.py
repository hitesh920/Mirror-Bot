"""Coverage for the shared resolver fetch helpers and migrated resolvers (#13)."""

import pytest
from aiohttp import ClientResponseError, ClientSession, web

from mirrorbot.resolvers.base import ResolvedDownload, fetch_json, fetch_text
from mirrorbot.resolvers.direct_hosts import SolidFilesResolver
from mirrorbot.resolvers.mediafire import MediaFireResolver


@pytest.fixture
async def server():
    app = web.Application()

    async def ok_text(_request):
        return web.Response(text="hello-body")

    async def ok_json(_request):
        return web.json_response({"value": 1})

    async def boom(_request):
        return web.Response(status=502)

    async def solidfiles_page(_request):
        return web.Response(
            text=(
                "<script>window.viewerOptions'"
                ', {"downloadUrl": "https://cdn.example/file.bin", '
                '"name": "file.bin"});</script>'
            )
        )

    async def mediafire_page(_request):
        return web.Response(
            text=(
                '<a aria-label="Download file" '
                'href="https://download.example/movie.mkv">Download</a>'
            )
        )

    app.router.add_get("/text", ok_text)
    app.router.add_route("*", "/json", ok_json)
    app.router.add_route("*", "/boom", boom)
    app.router.add_get("/solidfiles", solidfiles_page)
    app.router.add_get("/mediafire", mediafire_page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as session:
            yield f"http://127.0.0.1:{port}", session
    finally:
        await runner.cleanup()


async def test_fetch_text_returns_body(server):
    base, session = server
    assert await fetch_text(session, f"{base}/text") == "hello-body"


async def test_fetch_json_returns_parsed_body(server):
    base, session = server
    assert await fetch_json(session, f"{base}/json") == {"value": 1}


async def test_fetch_text_raises_for_http_error(server):
    base, session = server
    with pytest.raises(ClientResponseError):
        await fetch_text(session, f"{base}/boom")


async def test_fetch_json_supports_post(server):
    base, session = server
    assert await fetch_json(session, f"{base}/json", method="POST") == {"value": 1}


async def test_solidfiles_resolver_uses_helper(server):
    base, session = server
    result = await SolidFilesResolver().resolve(f"{base}/solidfiles", session)
    assert isinstance(result, ResolvedDownload)
    assert result.url == "https://cdn.example/file.bin"
    assert result.filename == "file.bin"


async def test_mediafire_resolver_uses_helper(server):
    base, session = server
    result = await MediaFireResolver().resolve(f"{base}/mediafire", session)
    assert result.url == "https://download.example/movie.mkv"
