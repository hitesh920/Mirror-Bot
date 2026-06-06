from html import escape

from .models import Task, TaskPhase


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def human_time(seconds: int) -> str:
    if seconds <= 0 or seconds >= 8_640_000:
        return "-"
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts[:2])


def progress_bar(progress: float) -> str:
    filled = min(10, max(0, int(progress * 10)))
    return "[" + "#" * filled + "-" * (10 - filled) + "]"


def field(label: str, value: str) -> str:
    return f"<b>{label}:</b> <code>{escape(value)}</code>"


def task_status(task: Task) -> str:
    name = (task.name or task.source.type.value).replace("\n", " ")[:70]
    lines = [field(task.phase.value.title(), name)]
    if task.phase == TaskPhase.SELECTING:
        lines.append("<i>Waiting for file selection</i>")
    elif task.phase == TaskPhase.METADATA:
        lines.append("<i>Waiting for metadata</i>")
    elif task.phase == TaskPhase.QUEUED:
        lines.append("<i>Waiting in download queue</i>")
    elif task.phase == TaskPhase.DOWNLOADING:
        if task.size:
            percent = f"{task.progress * 100:.1f}%"
        else:
            percent = "--"
        lines.append(f"<code>{progress_bar(task.progress)}</code> <b>{percent}</b>")
        lines.append(field("Processed", human_size(task.downloaded)))
        lines.append(field("Size", human_size(task.size) if task.size else "Unknown"))
        lines.append(field("Speed", f"{human_size(task.speed)}/s" if task.speed else "-"))
        lines.append(field("ETA", human_time(task.eta)))
    lines.append(field("ID", task.short_id()))
    return "\n".join(lines)


def format_status(tasks: list[Task]) -> str:
    if not tasks:
        return "No active tasks."
    sections = [task_status(task) for task in tasks]
    sections.append(field("Active tasks", str(len(tasks))))
    return "\n\n".join(sections)
