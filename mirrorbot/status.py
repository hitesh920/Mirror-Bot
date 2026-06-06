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
    filled = min(10, max(0, round(progress * 10)))
    return "[" + "#" * filled + "-" * (10 - filled) + "]"


def task_status(task: Task) -> str:
    name = (task.name or task.source.type.value).replace("\n", " ")[:70]
    lines = [f"{task.phase.value.title()} | {task.short_id()}", name]
    if task.phase == TaskPhase.SELECTING:
        lines.append("Waiting for file selection")
    elif task.phase == TaskPhase.METADATA:
        lines.append("Waiting for torrent metadata")
    elif task.phase == TaskPhase.QUEUED:
        lines.append("Waiting in download queue")
    elif task.phase == TaskPhase.DOWNLOADING:
        lines.append(f"{progress_bar(task.progress)} {task.progress * 100:.1f}%")
        transferred = human_size(task.downloaded)
        total = human_size(task.size) if task.size else "unknown"
        details = f"{transferred} / {total}"
        if task.speed:
            details += f" | {human_size(task.speed)}/s"
        if task.eta:
            details += f" | ETA {human_time(task.eta)}"
        lines.append(details)
    return "\n".join(lines)


def format_status(tasks: list[Task]) -> str:
    if not tasks:
        return "No active tasks."
    sections = [f"Active tasks: {len(tasks)}"]
    sections.extend(task_status(task) for task in tasks)
    return "\n\n".join(sections)
