from mirrorbot.core.models import TaskPhase


def test_task_phases_match_active_lifecycle():
    assert {phase.value for phase in TaskPhase} == {
        "queued",
        "fetching metadata",
        "selecting",
        "downloading",
        "preparing",
        "scanning",
        "extracting",
        "archiving",
        "splitting",
        "uploading",
        "complete",
        "cancelled",
        "error",
    }
