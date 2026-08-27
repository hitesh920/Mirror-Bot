"""The self-contained HTML page that lists the files in an uploaded R2 folder.

Kept apart from r2_delivery so the S3 client / listing / expiry logic is not
mixed in with an inline HTML/CSS/JS template.
"""

import re
from html import escape

from ..core.formatting import human_size

FOLDER_LABEL_PATTERN = re.compile(
    r'(?P<prefix><li data-file-name="(?P<name>[^"]+)" '
    r'data-file-url="[^"]+"><a href="[^"]+">Download</a><span>)'
    r"(?P<label>.*?)"
    r"(?P<suffix></span><small>)"
)


def build_folder_page(
    folder_name: str,
    files: list[tuple[str, str, int]],
    retention_seconds: int,
) -> bytes:
    rows = []
    for display_name, url, size in files:
        file_name = display_name.replace("\\", "/").rsplit("/", 1)[-1]
        rows.append(
            f'<li data-file-name="{escape(file_name, quote=True)}" '
            f'data-file-url="{escape(url, quote=True)}">'
            f'<a href="{escape(url, quote=True)}">Download</a>'
            f"<span>{escape(file_name)}</span>"
            f"<small>{human_size(size)}</small>"
            "</li>"
        )
    retention = (
        f"{retention_seconds // 86400} day"
        f"{'s' if retention_seconds // 86400 != 1 else ''}"
        if retention_seconds > 0 and retention_seconds % 86400 == 0
        else "the configured retention period"
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{escape(folder_name)}</title>
<style>
body{{margin:0;background:#0f172a;color:#e2e8f0;font:16px system-ui,sans-serif}}
main{{max-width:900px;margin:auto;padding:32px 18px}}
h1{{margin:0 0 8px;font-size:1.7rem;overflow-wrap:anywhere}}
p{{color:#94a3b8;margin:0 0 24px}}
.toolbar{{display:flex;align-items:center;gap:12px;margin:0 0 18px}}
.toolbar button{{border:0;border-radius:8px;padding:9px 14px;background:#2563eb;
color:#fff;font:inherit;font-weight:600;cursor:pointer}}
.toolbar button:hover{{background:#1d4ed8}}.toolbar button:focus-visible{{outline:3px solid #60a5fa}}
#copy-status{{color:#94a3b8;font-size:.9rem}}
ul{{list-style:none;padding:0;margin:0;display:grid;gap:10px}}
li{{display:grid;grid-template-columns:auto 1fr auto;gap:14px;align-items:center;
background:#1e293b;border:1px solid #334155;border-radius:12px;padding:14px}}
a{{color:#fff;background:#2563eb;padding:8px 12px;border-radius:8px;text-decoration:none}}
span{{overflow-wrap:anywhere}}small{{color:#94a3b8}}
@media(max-width:600px){{li{{grid-template-columns:1fr}}}}
</style>
</head>
<body><main>
<h1>{escape(folder_name)}</h1>
<p>{len(files)} file(s) · Automatically deleted after {escape(retention)}</p>
<div class="toolbar">
<button id="copy-all" type="button">Copy all</button>
<span id="copy-status" role="status" aria-live="polite"></span>
</div>
<ul>{"".join(rows)}</ul>
</main>
<script>
const copyButton = document.getElementById("copy-all");
const copyStatus = document.getElementById("copy-status");

async function writeClipboard(text) {{
  if (navigator.clipboard && window.isSecureContext) {{
    await navigator.clipboard.writeText(text);
    return;
  }}
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  const copied = document.execCommand("copy");
  area.remove();
  if (!copied) throw new Error("Clipboard copy was rejected");
}}

copyButton.addEventListener("click", async () => {{
  const entries = Array.from(document.querySelectorAll("li[data-file-url]"));
  const text = entries.map(
    (item) => item.dataset.fileName + "\\n" + item.dataset.fileUrl
  ).join("\\n\\n");
  try {{
    await writeClipboard(text);
    copyButton.textContent = "Copied!";
    copyStatus.textContent = `${{entries.length}} file links copied`;
    setTimeout(() => {{
      copyButton.textContent = "Copy all";
      copyStatus.textContent = "";
    }}, 2200);
  }} catch (error) {{
    copyStatus.textContent = "Copy failed. Allow clipboard access and try again.";
  }}
}});
</script>
</body>
</html>"""
    return document.encode("utf-8")


def normalize_folder_page_labels(document: bytes) -> tuple[bytes, int]:
    text = document.decode("utf-8")
    changes = 0

    def basename_label(match: re.Match) -> str:
        nonlocal changes
        if match.group("label") == match.group("name"):
            return match.group(0)
        changes += 1
        return f"{match.group('prefix')}{match.group('name')}{match.group('suffix')}"

    normalized = FOLDER_LABEL_PATTERN.sub(basename_label, text)
    return normalized.encode("utf-8"), changes
