"""Temporary token-protected pages for public Google Drive folders."""

import asyncio
import html
import json
import logging
import secrets
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from aiohttp import web

from .drive_sharing import DriveShareManifest
from .page_style import TEMP_PAGE_CSS

LOGGER = logging.getLogger(__name__)


@dataclass
class DriveSharePage:
    token: str
    manifest: DriveShareManifest
    expiry_job: asyncio.Task


class DriveSharePages:
    def __init__(self, public_base_url: str, port: int = 8004, timeout: int = 300):
        self.public_base_url = public_base_url.rstrip("/")
        self.port = port
        self.timeout = timeout
        self.lock = asyncio.Lock()
        self.pages: dict[str, DriveSharePage] = {}
        self.runner: web.AppRunner | None = None

    async def create(self, manifest: DriveShareManifest) -> str:
        async with self.lock:
            await self._start_server()
            token = secrets.token_urlsafe(32)
            page = DriveSharePage(
                token,
                manifest,
                asyncio.create_task(self._expire(token)),
            )
            self.pages[token] = page
            LOGGER.info(
                "Drive share page opened token=%s files=%s folders=%s",
                token[:8],
                len(manifest.files),
                manifest.folder_count,
            )
            return f"{self._public_url()}/share/{token}"

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
                LOGGER.info("Drive share page expired token=%s", token[:8])
                if not self.pages:
                    await self._stop_server()
        except asyncio.CancelledError:
            pass

    async def _start_server(self) -> None:
        if self.runner:
            return
        app = web.Application()
        app.router.add_get("/share/{token}", self._show)
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
            raise web.HTTPNotFound(text="Share page expired or not found.")
        return web.Response(
            text=render_share_page(page.manifest, self.timeout),
            content_type="text/html",
        )

    def _public_url(self) -> str:
        parsed = urlparse(self.public_base_url)
        host = parsed.hostname or "localhost"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return urlunparse(
            (
                parsed.scheme or "http",
                f"{host}:{self.port}",
                parsed.path.rstrip("/"),
                "",
                "",
                "",
            )
        ).rstrip("/")


def render_share_page(manifest: DriveShareManifest, timeout: int = 300) -> str:
    rows = []
    clipboard = []
    for index, item in enumerate(manifest.files, 1):
        clipboard.append(f"{item.name}\n{item.url}")
        rows.append(
            "<tr>"
            f"<td class='number'>{index}</td>"
            f"<td class='name'>{html.escape(item.name)}</td>"
            "<td class='action'>"
            f"<a href='{html.escape(item.url, quote=True)}' target='_blank' rel='noopener'>Download</a>"
            "</td></tr>"
        )
    clipboard_json = json.dumps("\n\n".join(clipboard)).replace("</", "<\\/")
    return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(manifest.name)}</title>
<style>
{TEMP_PAGE_CSS}
.tools{{margin-bottom:10px}}
.number{{width:52px;color:var(--muted)}}
.action{{width:116px}}
.action a{{display:inline-flex;align-items:center;justify-content:center;min-height:34px;border-radius:7px;background:var(--primary);color:#fff;padding:7px 10px;font-weight:760;text-decoration:none}}
#empty{{display:none}}
@media(max-width:700px){{th:nth-child(1),td:nth-child(1){{display:none}}}}
</style></head><body><header><div class="top">
<h1>{html.escape(manifest.name)}</h1>
<div class="meta"><span>{len(manifest.files)} files</span><span>{manifest.folder_count} folders</span><span id="timer">Expires in 5:00</span></div>
</div></header><main><div class="tools">
<input id="search" type="search" placeholder="Search files">
<button id="copy">Copy All Files and Links</button>
</div><table><thead><tr><th>#</th><th>File name</th><th>Link</th></tr></thead>
<tbody id="rows">{"".join(rows)}</tbody></table><div id="empty">No matching files</div></main><div id="toast">Copied to clipboard</div>
<script>
const copyText={clipboard_json},expires={timeout};
const toast=()=>{{const t=document.querySelector('#toast');t.style.display='block';setTimeout(()=>t.style.display='none',2200)}};
document.querySelector('#copy').onclick=async()=>{{try{{await navigator.clipboard.writeText(copyText)}}catch(e){{const x=document.createElement('textarea');x.value=copyText;document.body.appendChild(x);x.select();document.execCommand('copy');x.remove()}}toast()}};
document.querySelector('#search').oninput=e=>{{const q=e.target.value.toLowerCase();let shown=0;document.querySelectorAll('#rows tr').forEach(r=>{{r.hidden=!r.textContent.toLowerCase().includes(q);if(!r.hidden)shown++}});document.querySelector('#empty').style.display=shown?'none':'block'}};
let left=expires;setInterval(()=>{{left=Math.max(0,left-1);document.querySelector('#timer').textContent=`Expires in ${{Math.floor(left/60)}}:${{String(left%60).padStart(2,'0')}}`;if(!left){{window.close();document.body.innerHTML='<main><h2>Share page expired</h2></main>'}}}},1000);
</script></body></html>"""
