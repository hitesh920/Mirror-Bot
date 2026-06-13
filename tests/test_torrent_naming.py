from pathlib import Path

from mirrorbot.core.models import AddOptions, Destination, Source, SourceType, Task
from mirrorbot.services.media_library import MediaMatch
from mirrorbot.services.status import task_status


def make_task(tmp_path: Path, destination=Destination.LOCAL_SERIES):
    return Task(
        "torrent-id",
        1,
        1,
        1,
        Source(SourceType.MAGNET, "magnet:?xt=urn:btih:test"),
        destination,
        AddOptions(),
        tmp_path,
    )


def test_live_status_keeps_real_torrent_name(tmp_path):
    task = make_task(tmp_path)
    task.name = "Teach.You.a.Lesson.S01.MULTi.1080p.WEB.x264-FW"
    task.library_name = "Teach You a Lesson (2026)"

    status = task_status(task, 1)

    assert "Teach.You.a.Lesson.S01.MULTi.1080p.WEB.x264-FW" in status
    assert "Teach You a Lesson (2026)" not in status


def test_local_library_name_is_separate_from_torrent_name(tmp_path):
    task = make_task(tmp_path)
    task.name = "Teach.You.a.Lesson.S01.MULTi.1080p.WEB.x264-FW"
    match = MediaMatch("tv", "Teach You a Lesson", "2026", season=1)
    task.library_name = match.folder_name

    assert task.name == "Teach.You.a.Lesson.S01.MULTi.1080p.WEB.x264-FW"
    assert task.library_name == "Teach You a Lesson (2026)"


def test_telegram_and_drive_tasks_have_no_library_name(tmp_path):
    for destination in (Destination.TELEGRAM, Destination.GOOGLE_DRIVE):
        task = make_task(tmp_path, destination)
        task.name = "Real.Torrent.Name"
        assert task.name == "Real.Torrent.Name"
        assert task.library_name == ""
