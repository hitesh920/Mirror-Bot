from pathlib import Path

from mirrorbot.core.models import AddOptions, Destination, Source, SourceType, Task
from mirrorbot.services.media_library import MediaMatch, media_target, promote_yearless_series_folders
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


def test_series_delivery_promotes_existing_yearless_folder(tmp_path):
    series = tmp_path / "series"
    series.mkdir()
    existing = series / "Example Show"
    season = existing / "Season 01"
    season.mkdir(parents=True)
    (season / "episode-1.mkv").write_text("one")
    match = MediaMatch("tv", "Example Show", "2026", season=1, confidence=1)

    target = media_target(tmp_path, "series", Path("episode-2.mkv"), match)

    assert target == series / "Example Show (2026)" / "Season 01"
    assert not existing.exists()
    assert (series / "Example Show (2026)" / "Season 01" / "episode-1.mkv").is_file()


def test_series_delivery_does_not_merge_conflicting_yearless_folder(tmp_path):
    series = tmp_path / "series"
    series.mkdir()
    yearless = series / "Example Show"
    canonical = series / "Example Show (2026)"
    yearless.mkdir()
    canonical.mkdir()
    match = MediaMatch("tv", "Example Show", "2026", season=1, confidence=1)

    target = media_target(tmp_path, "series", Path("episode.mkv"), match)

    assert target == canonical / "Season 01"
    assert yearless.is_dir()


def test_startup_promotes_yearless_series_when_metadata_is_confident(tmp_path, monkeypatch):
    series = tmp_path / "series"
    movies = tmp_path / "movies"
    series.mkdir()
    movies.mkdir()
    existing = series / "Example Show"
    existing.mkdir()
    monkeypatch.setattr(
        "mirrorbot.services.media_library.resolve_media",
        lambda *_args: MediaMatch("tv", "Example Show", "2026", confidence=1),
    )

    stats = promote_yearless_series_folders(tmp_path, "key")

    assert stats == {"promoted": 1, "skipped": 0, "conflicts": 0}
    assert (series / "Example Show (2026)").is_dir()
