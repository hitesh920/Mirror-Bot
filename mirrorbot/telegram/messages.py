from datetime import UTC, datetime
from html import escape

from ..core.formatting import human_size
from ..core.models import Destination

ADD_USAGE = (
    "Usage: <code>/add &lt;link&gt; [-z|-zp password|-e|-ep password|-n name]</code>\n"
    "You can also reply to a Telegram file or link with <code>/add</code>.\n"
    "Batch: reply with <code>/add -b [message-count] [-n ZIP-name]</code>."
)

HELP_TEXT = "\n".join(
    [
        "<b>Mirror-Bot commands</b>",
        "",
        "<b>Add</b>",
        "<code>/add &lt;link&gt;</code> - add a link",
        "<code>/add</code> - use the replied file/link",
        "<code>/add -b</code> - batch links from the replied message",
        "<code>/add -b 3</code> - batch links from 3 exact messages",
        "<code>-z</code> zip, <code>-zp pass</code> password zip",
        "<code>-e</code> extract, <code>-ep pass</code> password extract",
        "<code>-n name</code> custom task name",
        "",
        "<b>Status</b>",
        "<code>/status</code> - live task status",
        "<code>/stats</code> - bot/server stats",
        "<code>/speedtest</code> - test server network speed",
        "<code>/r2stats</code> - Cloudflare R2 object usage",
        "",
        "<b>Manage</b>",
        "<code>/cancel &lt;task-id&gt;</code> - cancel one task",
        "<code>/cancelall</code> - cancel all active tasks",
        "<code>/restart</code> - gracefully restart Mirror-Bot",
        "<code>/logs</code> - send recent sanitized application logs",
        "<code>/delete &lt;key-or-link&gt;</code> - delete one R2 upload",
        "<code>/delete all</code> - delete all bot uploads from R2",
        "",
        "<b>Cloudflare R2</b>",
        "<code>/search &lt;name&gt;</code> - search current R2 uploads",
        "<code>/search *</code> - list all current R2 uploads",
        "Uploads are automatically deleted after their configured retention.",
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
            lines.append(
                f'<a href="{escape(links[index], quote=True)}">Open</a> - '
                f"<code>{safe_name}</code>"
            )
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


def retention_text(seconds: int) -> str:
    if seconds <= 0:
        return "Disabled"
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''}"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''}"
    minutes = max(1, seconds // 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def r2_expiry_warning_message(upload: dict) -> str:
    remaining = max(1, int(upload["remaining_seconds"]))
    hours, remainder = divmod(remaining, 3600)
    minutes = max(1, remainder // 60) if not hours else remainder // 60
    countdown = (
        f"{hours}h {minutes}m"
        if hours and minutes
        else (f"{hours}h" if hours else f"{minutes}m")
    )
    scheduled = datetime.fromtimestamp(upload["expires_at"], UTC).strftime(
        "%d %b %Y, %H:%M UTC"
    )
    kind = "Folder" if upload.get("kind") == "folder" else "File"
    sections = [
        "<b>Cloudflare R2 deletion warning</b>",
        "This upload is scheduled for automatic deletion within 12 hours.",
        "",
        f"<b>Name:</b> <code>{escape(str(upload['name'])[:120])}</code>",
        f"<b>Type:</b> <code>{kind}</code>",
        f"<b>Size:</b> <code>{human_size(int(upload.get('bytes') or 0))}</code>",
        f"<b>Deletes in:</b> <code>{countdown}</code>",
        f"<b>Scheduled:</b> <code>{scheduled}</code>",
        "",
        "Use <code>/search</code> if you still need its stored download link.",
    ]
    return "\n".join(sections)


def completion_message(task) -> str:
    name = escape(task.result_name or task.name or task.source.type.value)
    batch_sections = []
    if task.batch_total:
        batch_sections = [
            f"<b>Batch total:</b> <code>{task.batch_total}</code>",
            f"<b>Succeeded:</b> <code>{task.batch_completed}</code>",
            (
                "<b>Skipped:</b> "
                f"<code>{task.batch_failed + task.batch_initial_skipped}</code>"
            ),
            f"<b>Uploaded outputs:</b> <code>{len(task.result_files)}</code>",
        ]
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
            *batch_sections,
            result_list("Uploaded files", task.result_files, task.result_links),
            warning_list(task.processing_warnings),
        ]
    elif task.destination == Destination.CLOUDFLARE_R2:
        sections = [
            "<b>Task complete</b>",
            f"<b>Name:</b> <code>{name}</code>",
            "<b>Uploaded to:</b> <code>Cloudflare R2</code>",
            f"<b>Files:</b> <code>{len(task.result_files)}</code>",
            *batch_sections,
            (
                "<b>Automatically deleted after:</b> "
                f"<code>{retention_text(task.result_auto_delete_seconds)}</code>"
            ),
            warning_list(task.processing_warnings),
        ]
    else:
        raise ValueError(
            f"Unsupported completion destination: {task.destination.value}"
        )
    return "\n".join(section for section in sections if section)
