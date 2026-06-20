from html import escape

from ..core.models import Destination

ADD_USAGE = (
    "Usage: <code>/add &lt;link&gt; [-z|-zp password|-e|-ep password|-n name]</code>\n"
    "You can also reply to a Telegram file or link with <code>/add</code>."
)

HELP_TEXT = "\n".join(
    [
        "<b>Mirror-Bot commands</b>",
        "",
        "<b>Add</b>",
        "<code>/add &lt;link&gt;</code> - add a link",
        "<code>/add</code> - use the replied file/link",
        "<code>BuzzHeavier</code> links are supported as sources and uploads",
        "<code>-z</code> zip, <code>-zp pass</code> password zip",
        "<code>-e</code> extract, <code>-ep pass</code> password extract",
        "<code>-n name</code> custom task name",
        "",
        "<b>Status</b>",
        "<code>/status</code> - live task status",
        "<code>/stats</code> - bot/server stats",
        "<code>/speedtest</code> - test server network speed",
        "<code>/gdstats</code> - Google Drive auth and quota",
        "<code>/jellyfin</code> - manage Jellyfin",
        "<code>/local</code> - temporary local file explorer",
        "",
        "<b>Manage</b>",
        "<code>/cancel &lt;task-id&gt;</code> - cancel one task",
        "<code>/cancelall</code> - cancel all active tasks",
        "<code>/restart</code> - gracefully restart Mirror-Bot",
        "<code>/logs</code> - send recent sanitized application logs",
        "<code>/delete</code> - delete Local or Google Drive items",
        "<code>/delete &lt;drive-link-or-id&gt;</code> - delete Google Drive item",
        "",
        "<b>Google Drive</b>",
        "<code>/search &lt;name&gt;</code> - search Drive on a temporary page",
        "<code>/share &lt;drive-link&gt;</code> - temporary public Drive share page",
    ]
)


def format_jellyfin_status(status, jellyfin_url: str, action: str = "Status", server_info: dict | None = None) -> str:
    running = "yes" if status.running else "no"
    lines = [
        "<b>Jellyfin</b>",
        f"<b>Action:</b> <code>{escape(action)}</code>",
        f"<b>Container:</b> <code>{escape(status.name)}</code>",
        f"<b>State:</b> <code>{escape(status.state)}</code>",
        f"<b>Health:</b> <code>{escape(status.health)}</code>",
        f"<b>Running:</b> <code>{running}</code>",
    ]
    if server_info:
        lines.extend(
            [
                f"<b>Server:</b> <code>{escape(server_info.get('ServerName', 'unknown'))}</code>",
                f"<b>Version:</b> <code>{escape(server_info.get('Version', 'unknown'))}</code>",
            ]
        )
    lines.append(f"<b>URL:</b> <code>{escape(jellyfin_url)}</code>")
    return "\n".join(lines)


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
