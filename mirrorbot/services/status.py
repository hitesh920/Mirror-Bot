from html import escape

from ..core.models import Task, TaskPhase


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
    filled = min(12, max(0, int(progress * 12)))
    return "[" + "■" * filled + "□" * (12 - filled) + "]"


def field(label: str, value: str) -> str:
    return f"<b>{label}:</b> <code>{escape(value)}</code>"


def task_status(task: Task, number: int) -> str:
    name = (task.name or task.source.type.value).replace("\n", " ")[:70]
    lines = [f"<b>{number}.{escape(task.phase.value.title())}:</b> <code>{escape(name)}</code>"]
    current_file = task.current_file.replace("\n", " ")[:70]
    if current_file and current_file != name:
        lines.append(field("Current file", current_file))
    if task.phase == TaskPhase.SELECTING:
        lines.append("<i>Waiting for file selection</i>")
    elif task.phase == TaskPhase.METADATA:
        lines.append("<i>Waiting for metadata</i>")
    elif task.phase == TaskPhase.PREPARING:
        lines.append("<i>Preparing downloaded files</i>")
    elif task.phase == TaskPhase.SCANNING:
        lines.append("<i>Scanning files and folders</i>")
        lines.append(field("Files found", str(len(task.result_files))))
        lines.append(field("Folders found", str(len(task.result_folders))))
    elif task.phase in {
        TaskPhase.DOWNLOADING,
        TaskPhase.EXTRACTING,
        TaskPhase.ARCHIVING,
        TaskPhase.SPLITTING,
        TaskPhase.MOVING,
        TaskPhase.DELIVERING,
        TaskPhase.UPLOADING,
    }:
        if task.size:
            percent = f"{task.progress * 100:.1f}%"
        else:
            percent = "--"
        lines.append(f"<code>{progress_bar(task.progress)}</code> <b>{percent}</b>")
        if task.phase == TaskPhase.UPLOADING:
            processed_label = "Uploaded"
        elif task.phase == TaskPhase.MOVING:
            processed_label = "Moved"
        else:
            processed_label = "Processed"
        lines.append(field(processed_label, human_size(task.downloaded)))
        lines.append(field("Size", human_size(task.size) if task.size else "Unknown"))
        lines.append(field("Speed", f"{human_size(task.speed)}/s" if task.speed else "-"))
        lines.append(field("ETA", human_time(task.eta)))
    lines.append(field("ID", task.short_id()))
    return "\n".join(lines)


def brief_task_status(task: Task, number: int) -> str:
    name = (task.name or task.source.type.value).replace("\n", " ")[:35]
    progress = f"{task.progress * 100:.0f}%" if task.size else "--"
    return (
        f"<b>{number}.</b> {escape(name)} - "
        f"<b>{escape(task.phase.value.title())}</b> - "
        f"<code>{escape(progress)}</code> "
        f"- <code>{escape(task.short_id())}</code>"
    )


def format_status(tasks: list[Task]) -> str:
    if not tasks:
        return "No active tasks."
    sections = [task_status(task, index) for index, task in enumerate(tasks[:3], 1)]
    remaining = tasks[3:]
    if remaining:
        sections.append(
            "<b>Other active tasks:</b>\n"
            + "\n".join(
                brief_task_status(task, index)
                for index, task in enumerate(remaining, 4)
            )
        )
    sections.append(field("Active tasks", str(len(tasks))))
    return "\n\n".join(sections)
