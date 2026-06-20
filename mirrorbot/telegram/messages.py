from html import escape

from ..core.models import Destination


def result_list(title: str, items: list[str], links: list[str] | None = None) -> str:
    if not items:
        return ""
    limit = 20
    lines = [f"<b>{escape(title)}:</b>"]
    for index, name in enumerate(items[:limit]):
        safe_name = escape(name[:120])
        if links and index < len(links) and links[index]:
            lines.append(f'<a href="{escape(links[index], quote=True)}">Open</a> - <code>{safe_name}</code>')
        else:
            lines.append(f"<code>{safe_name}</code>")
    if len(items) > limit:
        lines.append(f"<i>...and {len(items) - limit} more</i>")
    return "\n".join(lines)


def completion_message(task) -> str:
    name = escape(task.result_name or task.name or task.source.type.value)
    if task.destination == Destination.TELEGRAM:
        sections = [
            "<b>Task complete</b>",
            f"<b>Name:</b> <code>{name}</code>",
            "<b>Uploaded to:</b> <code>Telegram</code>",
            f"<b>Files:</b> <code>{len(task.result_files)}</code>",
            result_list("Uploaded files", task.result_files, task.result_links),
        ]
    elif task.destination == Destination.GOOGLE_DRIVE:
        sections = [
            "<b>Task complete</b>",
            f"<b>Name:</b> <code>{name}</code>",
            "<b>Uploaded to:</b> <code>Google Drive</code>",
            f"<b>Files:</b> <code>{len(task.result_files)}</code>",
            f"<b>Folders:</b> <code>{len(task.result_folders)}</code>",
        ]
    elif task.destination == Destination.BUZZHEAVIER:
        sections = [
            "<b>Task complete</b>",
            f"<b>Name:</b> <code>{name}</code>",
            "<b>Uploaded to:</b> <code>BuzzHeavier</code>",
            f"<b>Files:</b> <code>{len(task.result_files)}</code>",
            result_list("BuzzHeavier links", task.result_files, task.result_links),
        ]
    else:
        local_name = escape(task.library_name or task.result_name or task.name or task.source.type.value)
        sections = [
            "<b>Task complete</b>",
            f"<b>Name:</b> <code>{local_name}</code>",
            f"<b>Uploaded to:</b> <code>{escape(str(task.result_path or 'Local'))}</code>",
            f"<b>Files:</b> <code>{len(task.result_files)}</code>",
            f"<b>Folders:</b> <code>{len(task.result_folders)}</code>",
            result_list("Files", task.result_files),
            result_list("Folders", task.result_folders),
        ]
    return "\n".join(section for section in sections if section)


def completion_payload(task, jellyfin_url: str) -> dict:
    links = []
    if task.destination == Destination.GOOGLE_DRIVE and task.result_links:
        links.append({"label": "Open Google Drive", "url": task.result_links[0]})
    elif task.destination == Destination.BUZZHEAVIER:
        links.extend(
            {"label": f"Open {index}", "url": link}
            for index, link in enumerate(task.result_links[:10], start=1)
        )
    elif task.destination in {Destination.LOCAL_MOVIES, Destination.LOCAL_SERIES}:
        links.append({"label": "Open Jellyfin", "url": jellyfin_url})
    name = (
        task.library_name or task.result_name or task.name or task.source.type.value
        if task.destination in {Destination.LOCAL_MOVIES, Destination.LOCAL_SERIES}
        else task.result_name or task.name or task.source.type.value
    )
    return {
        "name": name,
        "destination": task.destination.value,
        "files": task.result_files,
        "folders": task.result_folders,
        "links": links,
        "path": str(task.result_path or ""),
    }
