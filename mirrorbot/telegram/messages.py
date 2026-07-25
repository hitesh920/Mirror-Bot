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
        "<code>/r2stats</code> - Cloudflare R2 object usage",
        "",
        "<b>Manage</b>",
        "<code>/cancel &lt;task-id&gt;</code> - cancel one task",
        "<code>/cancelall</code> - cancel all active tasks",
        "<code>/restart</code> - gracefully restart Mirror-Bot",
        "<code>/logs</code> - send recent sanitized application logs",
        "<code>/delete</code> - delete a Google Drive item",
        "<code>/delete &lt;drive-link-or-id&gt;</code> - delete Google Drive item",
        "<code>/delete all</code> - empty the configured Drive upload folder",
        "<code>/r2delete &lt;key-or-link&gt;</code> - delete one R2 object",
        "<code>/r2delete all</code> - delete all bot uploads from R2",
        "",
        "<b>Google Drive</b>",
        "<code>/search &lt;name&gt;</code> - search Drive on a temporary page",
        "<code>/share &lt;drive-link&gt;</code> - temporary public Drive share page",
        "",
        "<b>Cloudflare R2</b>",
        "<code>/r2search &lt;name&gt;</code> - search current R2 uploads",
        "R2 download links expire after <code>24 hours</code>.",
    ]
)


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


def warning_list(items: list[str]) -> str:
    if not items:
        return ""
    lines = ["<b>Warnings:</b>"]
    lines.extend(f"<code>{escape(item[:160])}</code>" for item in items[:5])
    if len(items) > 5:
        lines.append(f"<i>...and {len(items) - 5} more</i>")
    return "\n".join(lines)


def completion_message(task) -> str:
    name = escape(task.result_name or task.name or task.source.type.value)
    if task.destination == Destination.TELEGRAM:
        upload_label = (
            "Telegram dump channel"
            if getattr(task, "telegram_upload_mode", "") == "dump_channel"
            else "Telegram"
        )
        sections = [
            "<b>Task complete</b>",
            f"<b>Name:</b> <code>{name}</code>",
            f"<b>Uploaded to:</b> <code>{upload_label}</code>",
            f"<b>Files:</b> <code>{len(task.result_files)}</code>",
            result_list("Uploaded files", task.result_files, task.result_links),
            warning_list(task.processing_warnings),
        ]
    elif task.destination == Destination.GOOGLE_DRIVE:
        sections = [
            "<b>Task complete</b>",
            f"<b>Name:</b> <code>{name}</code>",
            "<b>Uploaded to:</b> <code>Google Drive</code>",
            f"<b>Files:</b> <code>{len(task.result_files)}</code>",
            f"<b>Folders:</b> <code>{len(task.result_folders)}</code>",
            warning_list(task.processing_warnings),
        ]
    elif task.destination == Destination.BUZZHEAVIER:
        sections = [
            "<b>Task complete</b>",
            f"<b>Name:</b> <code>{name}</code>",
            "<b>Uploaded to:</b> <code>BuzzHeavier</code>",
            f"<b>Files:</b> <code>{len(task.result_files)}</code>",
            result_list("BuzzHeavier links", task.result_files, task.result_links),
            warning_list(task.processing_warnings),
        ]
    elif task.destination == Destination.CLOUDFLARE_R2:
        button_count = min(10, len(task.result_links))
        sections = [
            "<b>Task complete</b>",
            f"<b>Name:</b> <code>{name}</code>",
            "<b>Uploaded to:</b> <code>Cloudflare R2</code>",
            f"<b>Files:</b> <code>{len(task.result_files)}</code>",
            "<b>Links expire:</b> <code>24 hours</code>",
            f"<b>Download buttons:</b> <code>{button_count}</code>",
            (
                "Use <code>/r2search &lt;name&gt;</code> for additional files."
                if len(task.result_links) > button_count
                else ""
            ),
            warning_list(task.processing_warnings),
        ]
    else:
        raise ValueError(f"Unsupported completion destination: {task.destination.value}")
    return "\n".join(section for section in sections if section)
