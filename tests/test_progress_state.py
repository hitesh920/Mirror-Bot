"""Coverage for the atomic Task progress helpers (N7)."""

from mirrorbot.core import models


def test_begin_progress_resets_all_counters(make_task):
    task = make_task()
    task.downloaded = 500
    task.speed = 99
    task.eta = 42
    task.progress = 0.7

    task.begin_progress(2000)

    assert task.size == 2000
    assert task.downloaded == 0
    assert task.speed == 0
    assert task.eta == 0
    assert task.progress == 0.0


def test_report_progress_derives_speed_eta_and_fraction(make_task, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(models, "monotonic", lambda: clock["t"])
    task = make_task()
    task.begin_progress(1000)

    clock["t"] = 1010.0  # 10s elapsed
    task.report_progress(400)

    assert task.downloaded == 400
    assert task.progress == 0.4
    assert task.speed == 40  # 400 bytes / 10s
    assert task.eta == 15  # (1000-400)/40


def test_report_progress_without_size_stays_zero_until_complete(make_task):
    task = make_task()
    task.begin_progress(0)

    task.report_progress(123)
    assert task.progress == 0.0

    task.report_progress(123, complete=True)
    assert task.progress == 1.0
    assert task.size == 123
    assert task.downloaded == 123
    assert task.eta == 0


def test_report_progress_complete_snaps_to_size(make_task):
    task = make_task()
    task.begin_progress(1000)
    task.report_progress(950, complete=True)

    assert task.downloaded == 1000
    assert task.progress == 1.0


def test_advance_progress_accumulates(make_task):
    task = make_task()
    task.begin_progress(300)
    task.advance_progress(100)
    task.advance_progress(50)

    assert task.downloaded == 150
    assert task.progress == 0.5


def test_report_progress_never_exceeds_one(make_task):
    task = make_task()
    task.begin_progress(100)
    task.report_progress(250)

    assert task.progress == 1.0
    assert task.eta == 0


def test_set_transfer_stats_stores_engine_values_verbatim(make_task):
    task = make_task()
    task.set_transfer_stats(downloaded=500, size=1000, speed=250, eta=2, progress=0.5)

    assert (task.downloaded, task.size, task.speed, task.eta) == (500, 1000, 250, 2)
    assert task.progress == 0.5


def test_set_transfer_stats_derives_progress_when_absent(make_task):
    task = make_task()
    task.set_transfer_stats(downloaded=750, size=1000, speed=0, eta=0)

    assert task.progress == 0.75
