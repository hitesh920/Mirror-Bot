import asyncio
import html
import logging
import secrets
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from aiohttp import web

from .google_drive_delivery import FOLDER_MIME_TYPE, drive_item_link
from .page_style import TEMP_PAGE_CSS
from .status import human_size

LOGGER = logging.getLogger(__name__)


@dataclass
class DriveSearchPage:
    token: str
    query: str
    results: list[dict]
    expiry_job: asyncio.Task


class DriveSearchPages:
    def __init__(self, public_base_url: str, port: int, timeout: int = 300):
        self.public_base_url = public_base_url.rstrip("/")
        self.port = port
        self.timeout = timeout
        self.lock = asyncio.Lock()
        self.pages: dict[str, DriveSearchPage] = {}
        self.runner: web.AppRunner | None = None

    async def create(self, query: str, results: list[dict]) -> str:
        async with self.lock:
            await self._start_server()
            token = secrets.token_urlsafe(32)
            page = DriveSearchPage(
                token,
                query,
                results,
                asyncio.create_task(self._expire(token)),
            )
            self.pages[token] = page
            LOGGER.info("Drive search page opened token=%s results=%s", token[:8], len(results))
            return f"{self._public_url()}/drive-search/{token}"

    async def close_all(self) -> None:
        async with self.lock:
            for page in self.pages.values():
                page.expiry_job.cancel()
            self.pages.clear()
            await self._stop_server()

    async def _expire(self, token: str) -> None:
        try:
            await asyncio.sleep(self.timeout)
            async with self.lock:
                self.pages.pop(token, None)
                LOGGER.info("Drive search page expired token=%s", token[:8])
                if not self.pages:
                    await self._stop_server()
        except asyncio.CancelledError:
            pass

    async def _start_server(self) -> None:
        if self.runner:
            return
        app = web.Application()
        app.router.add_get("/drive-search/{token}", self._show)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "0.0.0.0", self.port).start()

    async def _stop_server(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    async def _show(self, request: web.Request) -> web.Response:
        token = request.match_info.get("token", "")
        page = self.pages.get(token)
        if page is None or not secrets.compare_digest(token, page.token):
            raise web.HTTPNotFound()
        return web.Response(
            text=render_search_page(page.query, page.results),
            content_type="text/html",
        )

    def _public_url(self) -> str:
        parsed = urlparse(self.public_base_url)
        host = parsed.hostname or "localhost"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = f"{host}:{self.port}"
        if parsed.username:
            auth = parsed.username
            if parsed.password:
                auth += f":{parsed.password}"
            netloc = f"{auth}@{netloc}"
        return urlunparse(
            (
                parsed.scheme or "http",
                netloc,
                parsed.path.rstrip("/"),
                "",
                "",
                "",
            )
        ).rstrip("/")


def render_search_page(query: str, results: list[dict]) -> str:
    rows = []
    for index, item in enumerate(results, 1):
        name = html.escape(item.get("name") or "Untitled")
        mime_type = item.get("mimeType", "")
        kind = "Folder" if mime_type == FOLDER_MIME_TYPE else "File"
        size = human_size(int(item.get("size") or 0)) if item.get("size") is not None else "-"
        link = html.escape(drive_item_link(item), quote=True)
        badge = "DIR" if kind == "Folder" else "FILE"
        rows.append(
            "<tr class='row'>"
            f"<td class='cell number'>{index}</td>"
            "<td class='cell'><div class='file-main'>"
            f"<span class='file-icon {kind.lower()}'>{badge}</span>"
            f"<div class='file-name'><strong>{name}</strong><span>{kind}</span></div>"
            "</div></td>"
            f"<td class='cell size'>{html.escape(size)}</td>"
            f"<td class='cell action'><a class='primary-link' href='{link}' target='_blank' rel='noopener'>Open</a></td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Google Drive search</title>
<style>
{TEMP_PAGE_CSS}
body{{background:var(--bg)}}
.appbar{{position:sticky;top:0;z-index:8;border-bottom:1px solid var(--line);background:color-mix(in srgb,var(--surface) 94%,transparent);backdrop-filter:blur(14px)}}
.appbar-inner{{max-width:1180px;margin:0 auto;padding:14px 18px;display:flex;align-items:center;justify-content:space-between;gap:14px}}
.brand{{display:grid;gap:5px;min-width:0}}.brand h1{{font-size:22px;margin:0}}.brand p{{margin:0;color:var(--muted);overflow-wrap:anywhere}}
.meta-pills{{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}}.meta-pills span{{display:inline-flex;align-items:center;min-height:34px;border:1px solid var(--line);border-radius:999px;background:var(--surface-soft);padding:6px 10px;color:var(--muted);font-weight:760;white-space:nowrap}}
.shell{{max-width:1180px;margin:0 auto;padding:16px 18px 28px;display:grid;gap:12px}}
.toolbar,.results-card{{border:1px solid var(--line);border-radius:10px;background:var(--surface);box-shadow:var(--shadow)}}
.toolbar{{padding:10px;display:flex;gap:10px;align-items:center}}.toolbar input{{flex:1;min-width:220px}}
.results-card{{overflow:hidden}}.table-wrap{{overflow:auto}}table{{border:0;border-radius:0;box-shadow:none;min-width:640px}}th,td{{padding:0;border-bottom:1px solid var(--line)}}th{{height:42px;padding:0 14px}}.row:hover{{background:var(--surface-soft)}}.cell{{padding:11px 14px}}.number{{width:56px;color:var(--muted)}}.size{{width:120px;color:var(--muted);white-space:nowrap}}.action{{width:104px;text-align:right}}
.file-main{{display:flex;align-items:center;gap:12px;min-width:0}}.file-icon{{width:34px;height:34px;border:1px solid var(--line);border-radius:8px;background:var(--surface-soft);display:inline-flex;align-items:center;justify-content:center;color:var(--muted);font-size:10px;font-weight:900;letter-spacing:.03em;flex:0 0 auto}}.file-icon.folder{{background:var(--primary-soft);color:var(--primary);border-color:color-mix(in srgb,var(--primary) 25%,var(--line))}}.file-name{{display:grid;gap:2px;min-width:0}}.file-name strong{{overflow-wrap:anywhere}}.file-name span{{font-size:12px;color:var(--muted)}}
.primary-link{{display:inline-flex;align-items:center;justify-content:center;min-height:32px;border-radius:7px;background:var(--primary);color:#fff;padding:6px 10px;font-weight:760;text-decoration:none}}#empty{{display:none;border:1px solid var(--line);border-radius:10px;background:var(--surface);padding:38px;text-align:center;color:var(--muted)}}
@media(max-width:700px){{.appbar-inner{{display:grid;padding:12px}}.meta-pills{{justify-content:flex-start}}.shell{{padding:12px 10px 22px}}.number,th:nth-child(1),.size,th:nth-child(3){{display:none}}table{{min-width:480px}}.cell{{padding:10px}}}}
</style></head><body>
<header class="appbar"><div class="appbar-inner"><div class="brand"><h1>Google Drive search</h1><p>Query: {html.escape(query)}</p></div><div class="meta-pills"><span>{len(results)} results</span><span>Expires in 5 minutes</span></div></div></header>
<main class="shell"><section class="toolbar"><input id="filter" type="search" placeholder="Search results"></section>
<section class="results-card"><div class="table-wrap"><table><thead><tr><th>#</th><th>Name</th><th>Size</th><th></th></tr></thead><tbody id="rows">{"".join(rows)}</tbody></table></div></section><div id="empty">No matching results</div></main>
<script>document.querySelector('#filter').oninput=e=>{{const q=e.target.value.toLowerCase();let shown=0;document.querySelectorAll('#rows tr').forEach(r=>{{r.hidden=!r.textContent.toLowerCase().includes(q);if(!r.hidden)shown++}});document.querySelector('#empty').style.display=shown?'none':'block'}}</script>
</body></html>"""
