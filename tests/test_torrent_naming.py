import pytest

from mirrorbot.core.models import AddOptions, Destination, Source, SourceType
from mirrorbot.services.media_library import MediaMatch
from mirrorbot.services.task_manager import TaskManager


@pytest.mark.asyncio
async def test_series_torrent_gets_canonical_display_name_from_episode(config, monkeypatch):
    manager = TaskManager(config)
    task = manager.create_task(
        1,
        1,
        1,
        Source(SourceType.MAGNET, "magnet:?xt=urn:btih:test"),
        Destination.LOCAL_SERIES,
        AddOptions(),
    )
    calls = []

    def resolve(name, category, api_key):
        calls.append((name, category, api_key))
        return MediaMatch("tv", "Teach You a Lesson", "2026", season=1)

    monkeypatch.setattr("mirrorbot.services.task_manager.resolve_media", resolve)
    await manager._prepare_torrent_display_name(
        task,
        {"name": "cccc2128626038838e9a3d903dee3fae2f087230"},
        [
            {"name": "Teach.You.a.Lesson.S01E01.1080p.WEB.h264.mkv", "size": 100},
            {"name": "sample.txt", "size": 1},
        ],
    )

    assert calls[0][0].endswith("S01E01.1080p.WEB.h264.mkv")
    assert calls[0][1] == "series"
    assert task.name == "Teach You a Lesson (2026)"
    await manager.qb.close()


@pytest.mark.asyncio
async def test_custom_torrent_name_is_preserved(config, monkeypatch):
    manager = TaskManager(config)
    task = manager.create_task(
        1,
        1,
        1,
        Source(SourceType.MAGNET, "magnet:?xt=urn:btih:test"),
        Destination.LOCAL_MOVIES,
        AddOptions(name="My custom name"),
    )

    def unexpected(*_args):
        raise AssertionError("TMDb should not run for a custom name")

    monkeypatch.setattr("mirrorbot.services.task_manager.resolve_media", unexpected)
    await manager._prepare_torrent_display_name(task, {"name": "hash"}, [])

    assert task.options.name == "My custom name"
    await manager.qb.close()