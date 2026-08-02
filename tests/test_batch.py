import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiohttp import web

from mirrorbot.core.batch import (
    MAX_BATCH_LINKS,
    collect_batch_sources,
    extract_message_urls,
)
from mirrorbot.core.models import (
    AddOptions,
    Destination,
    Source,
    SourceType,
    Task,
    TaskPhase,
)
from mirrorbot.core.parser import parse_add_text
from mirrorbot.downloaders import batch as batch_downloader
from mirrorbot.downloaders.batch import BatchDownloadError, download_batch
from mirrorbot.services import archive
from mirrorbot.services.status import task_status
from mirrorbot.telegram.keyboards import batch_mode_buttons
from mirrorbot.telegram.messages import completion_message


def make_batch_task(tmp_path: Path, count: int = 3) -> Task:
    sources = [
        Source(
            SourceType.DIRECT_URL,
            f"https://example.com/file-{index}.bin",
            metadata={"batch_direct_only": True},
        )
        for index in range(1, count + 1)
    ]
    return Task(
        id=str(uuid4()),
        user_id=1,
        chat_id=1,
        message_id=77,
        source=Source(SourceType.BATCH, "", metadata={"sources": sources}),
        destination=Destination.CLOUDFLARE_R2,
        options=AddOptions(name="My Batch", zip=True, batch_messages=1),
        work_dir=tmp_path / "task",
        batch_total=count,
    )


@pytest.mark.parametrize(
    ("command", "count", "name"),
    [
        ("/add -b", 1, ""),
        ("/add -b 3", 3, ""),
        ('/add -b 3 -n "My Batch"', 3, "My Batch"),
    ],
)
def test_parse_batch_options(command, count, name):
    link, options = parse_add_text(command)

    assert link == ""
    assert options.batch_messages == count
    assert options.name == name


@pytest.mark.parametrize(
    "command",
    [
        "/add -b 0",
        "/add -b 21",
        "/add -b many",
        "/add -b -b",
        "/add -b -z",
        "/add -b -zp secret",
        "/add -b -e",
        "/add -b -ep secret",
    ],
)
def test_parse_batch_rejects_invalid_or_conflicting_options(command):
    with pytest.raises(ValueError):
        parse_add_text(command)


def test_normal_add_and_magnet_parsing_are_unchanged():
    link, options = parse_add_text("/add https://example.com/file.bin -n saved.bin")
    assert link == "https://example.com/file.bin"
    assert options.name == "saved.bin"
    assert options.batch_messages == 0

    magnet_with_batch_text = "magnet:?xt=urn:btih:abc&dn=Release -b"
    link, options = parse_add_text(f"/add {magnet_with_batch_text}")
    assert link == magnet_with_batch_text.replace(" ", "%20")
    assert options.batch_messages == 0

    magnet = "magnet:?xt=urn:btih:abc&dn=Release - Name"
    link, options = parse_add_text(f"/add {magnet}")
    assert link == magnet.replace(" ", "%20")
    assert options.batch_messages == 0


def test_extract_message_urls_includes_visible_caption_and_text_links():
    message = SimpleNamespace(
        text=None,
        caption=(
            "First hidden link, then https://example.com/one.bin and "
            "https://example.com/three.bin"
        ),
        caption_entities=[SimpleNamespace(url="https://example.com/two.bin", offset=6)],
    )

    assert extract_message_urls(message) == [
        "https://example.com/two.bin",
        "https://example.com/one.bin",
        "https://example.com/three.bin",
    ]


def test_batch_collection_preserves_order_and_skips_duplicates_and_missing():
    messages = [
        SimpleNamespace(
            text=(
                "https://example.com/one.bin\n"
                "https://example.com/one.bin\n"
                "https://t.me/channel/1\n"
                "https:///missing-host"
            ),
            entities=[],
        ),
        None,
        SimpleNamespace(
            text="https://example.com/two.bin magnet:?xt=urn:btih:abc",
            entities=[],
        ),
    ]

    result = collect_batch_sources(messages)

    assert [source.value for source in result.sources] == [
        "https://example.com/one.bin",
        "https://example.com/two.bin",
    ]
    assert result.skipped == 4
    assert result.missing_messages == 1
    assert all(source.metadata["batch_direct_only"] for source in result.sources)


def test_batch_collection_caps_valid_links_at_twenty():
    text = "\n".join(
        f"https://example.com/{index}.bin" for index in range(MAX_BATCH_LINKS + 3)
    )
    result = collect_batch_sources([SimpleNamespace(text=text, entities=[])])

    assert len(result.sources) == MAX_BATCH_LINKS
    assert result.skipped == 3


@pytest.mark.asyncio
async def test_batch_downloader_limits_concurrency_and_keeps_partial_success(
    tmp_path, monkeypatch
):
    task = make_batch_task(tmp_path, count=6)
    active = 0
    maximum_active = 0

    async def fake_resolve(source):
        return source

    async def fake_download(child):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            await asyncio.sleep(0.02)
            if child.source.value.endswith("file-4.bin"):
                raise RuntimeError("temporary failure at https://secret.example/file")
            child.work_dir.mkdir(parents=True, exist_ok=True)
            target = child.work_dir / "same.bin"
            target.write_bytes(child.source.value.encode())
            child.downloaded = target.stat().st_size
            child.size = child.downloaded
            return target
        finally:
            active -= 1

    monkeypatch.setattr(batch_downloader, "resolve_source", fake_resolve)
    monkeypatch.setattr(batch_downloader, "download_direct", fake_download)

    root = await download_batch(task)

    assert maximum_active == 3
    assert task.batch_completed == 5
    assert task.batch_failed == 1
    assert len(list(root.glob("*.bin"))) == 5
    assert (root / "same.bin").is_file()
    assert (root / "same (2).bin").is_file()
    assert "https://" not in task.processing_warnings[0]


@pytest.mark.asyncio
async def test_batch_downloader_preserves_collection_folder(tmp_path, monkeypatch):
    task = make_batch_task(tmp_path, count=2)

    async def fake_resolve(source):
        return source

    async def fake_download(child):
        folder = child.work_dir / "Collection"
        nested = folder / "Season 1"
        nested.mkdir(parents=True)
        (nested / "episode.mkv").write_bytes(b"episode")
        child.downloaded = 7
        child.size = 7
        return folder

    monkeypatch.setattr(batch_downloader, "resolve_source", fake_resolve)
    monkeypatch.setattr(batch_downloader, "download_direct", fake_download)

    root = await download_batch(task)

    assert (root / "Collection" / "Season 1" / "episode.mkv").is_file()
    assert (root / "Collection (2)" / "Season 1" / "episode.mkv").is_file()


@pytest.mark.asyncio
async def test_batch_downloader_fails_when_every_source_fails(tmp_path, monkeypatch):
    task = make_batch_task(tmp_path, count=2)

    async def fake_resolve(source):
        return source

    async def fake_download(_child):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(batch_downloader, "resolve_source", fake_resolve)
    monkeypatch.setattr(batch_downloader, "download_direct", fake_download)

    with pytest.raises(BatchDownloadError, match="Every link"):
        await download_batch(task)

    assert task.batch_failed == 2


@pytest.mark.asyncio
async def test_batch_downloads_real_http_sources_end_to_end(tmp_path):
    application = web.Application()

    async def file_one(_request):
        return web.Response(
            body=b"one",
            headers={"Content-Disposition": 'attachment; filename="one.bin"'},
        )

    async def file_two(_request):
        return web.Response(
            body=b"two",
            headers={"Content-Disposition": 'attachment; filename="two.bin"'},
        )

    application.router.add_get("/first", file_one)
    application.router.add_get("/second", file_two)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    task = make_batch_task(tmp_path, count=2)
    task.source.metadata["sources"] = [
        Source(SourceType.DIRECT_URL, f"http://127.0.0.1:{port}/first"),
        Source(SourceType.DIRECT_URL, f"http://127.0.0.1:{port}/second"),
    ]
    try:
        root = await download_batch(task)
    finally:
        await runner.cleanup()

    assert (root / "one.bin").read_bytes() == b"one"
    assert (root / "two.bin").read_bytes() == b"two"
    assert task.batch_completed == 2
    assert task.batch_failed == 0


@pytest.mark.asyncio
async def test_batch_cancellation_removes_child_workspaces(tmp_path, monkeypatch):
    task = make_batch_task(tmp_path, count=2)
    started = asyncio.Event()

    async def fake_resolve(source):
        return source

    async def fake_download(child):
        child.work_dir.mkdir(parents=True, exist_ok=True)
        (child.work_dir / "partial.bin").write_bytes(b"partial")
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(batch_downloader, "resolve_source", fake_resolve)
    monkeypatch.setattr(batch_downloader, "download_direct", fake_download)
    operation = asyncio.create_task(download_batch(task))
    await started.wait()
    operation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await operation

    assert not (task.work_dir / ".batch-parts").exists()


@pytest.mark.asyncio
async def test_batch_zip_uses_store_mode_and_flattens_staging_root(
    tmp_path, monkeypatch
):
    root = tmp_path / "Batch"
    root.mkdir()
    (root / "one.bin").write_bytes(b"one")
    task = make_batch_task(tmp_path)
    captured = {}

    async def fake_run(_task, *command, cwd=None):
        captured["command"] = command
        captured["cwd"] = cwd

    monkeypatch.setattr(archive, "_run", fake_run)

    output = await archive.zip_path(root, task, level=0, contents_only=True)

    assert output == tmp_path / "Batch.zip"
    assert "-mx=0" in captured["command"]
    assert captured["command"][-1] == "."
    assert captured["cwd"] == root


def test_batch_ui_status_and_completion_summary():
    task = make_batch_task(Path("unused"), count=3)
    task.phase = TaskPhase.DOWNLOADING
    task.batch_completed = 2
    task.batch_failed = 1
    task.batch_initial_skipped = 2
    task.downloaded = 2_000
    task.result_name = "My Batch.zip"
    task.result_files = ["My Batch.zip"]

    buttons = batch_mode_buttons("77").inline_keyboard
    assert [button.text for button in buttons[0]] == ["Separate uploads", "ZIP upload"]
    status = task_status(task, 1)
    assert "2 / 3 completed" in status
    assert "Failed:</b> <code>1" in status
    complete = completion_message(task)
    assert "Batch total:</b> <code>3" in complete
    assert "Succeeded:</b> <code>2" in complete
    assert "Skipped:</b> <code>3" in complete
    assert "Uploaded outputs:</b> <code>1" in complete
