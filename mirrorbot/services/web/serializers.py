from ..status import human_size
from ...core.models import Destination, Task


def human_duration(seconds) -> str:
    if seconds in (None, "", "-"):
        return "-"
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return str(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def display_name(task: Task) -> str:
    if task.terminal:
        if task.destination in {Destination.LOCAL_MOVIES, Destination.LOCAL_SERIES}:
            return task.library_name or task.result_name or task.name or task.source.filename or task.source.type.value
        return task.result_name or task.name or task.source.filename or task.source.type.value
    return task.name or task.source.filename or task.result_name or task.source.type.value


def task_json(task: Task, completion_payload) -> dict:
    return {
        "id": task.short_id(),
        "full_id": task.id,
        "name": display_name(task),
        "phase": task.phase.value,
        "source": task.source.type.value,
        "destination": task.destination.value,
        "current_file": task.current_file,
        "progress": round(task.progress * 100, 1) if task.size else None,
        "size": human_size(task.size) if task.size else "Unknown",
        "processed": human_size(task.downloaded),
        "speed": f"{human_size(task.speed)}/s" if task.speed else "-",
        "eta": human_duration(task.eta),
        "error": task.error,
        "terminal": task.terminal,
        "selection_url": task.selection_url if task.phase.value == "selecting" and not task.terminal else "",
        "result": completion_payload(task) if task.terminal else None,
    }
