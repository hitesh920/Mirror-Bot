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
        rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{kind}</td>"
            f"<td class='name'>{name}</td>"
            f"<td>{html.escape(size)}</td>"
            f"<td><a href='{link}' target='_blank' rel='noopener'>Open</a></td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Google Drive search</title>
<style>
{TEMP_PAGE_CSS}
.tools{{position:static;margin-bottom:12px;border:0;background:transparent;backdrop-filter:none;padding:0}}
.name{{font-weight:760}}
a{{display:inline-flex;align-items:center;justify-content:center;min-height:34px;border-radius:7px;background:var(--primary);color:#fff;padding:7px 10px;font-weight:760;text-decoration:none}}
.empty{{display:none}}
@media(max-width:640px){{th:nth-child(1),td:nth-child(1),th:nth-child(4),td:nth-child(4){{display:none}}}}
</style></head><body><header><div class="top"><h1>Google Drive search</h1><div class="meta"><span>Query: {html.escape(query)}</span><span>{len(results)} results</span><span>Expires in 5 minutes</span></div></div></header><main>
<div class="tools"><input id="filter" type="search" placeholder="Filter results"></div>
<table><thead><tr><th>#</th><th>Type</th><th>Name</th><th>Size</th><th>Link</th></tr></thead>
<tbody id="rows">{"".join(rows)}</tbody></table><div class="empty" id="empty">No matching results</div>
<script>document.querySelector('#filter').oninput=e=>{{const q=e.target.value.toLowerCase();let shown=0;document.querySelectorAll('#rows tr').forEach(r=>{{r.hidden=!r.textContent.toLowerCase().includes(q);if(!r.hidden)shown++}});document.querySelector('#empty').style.display=shown?'none':'block'}}</script>
</main></body></html>"""
