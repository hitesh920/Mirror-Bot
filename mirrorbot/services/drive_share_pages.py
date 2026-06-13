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
*{{box-sizing:border-box}}body{{font:15px system-ui;margin:0;background:#f4f6f8;color:#182230}}
header{{background:#fff;border-bottom:1px solid #dfe4ea}}.top{{max-width:1120px;margin:auto;padding:22px 18px 14px}}
h1{{font-size:22px;margin:0 0 6px;overflow-wrap:anywhere}}.meta{{color:#667085;display:flex;gap:18px;flex-wrap:wrap}}
main{{max-width:1120px;margin:18px auto;padding:0 18px}}.tools{{position:sticky;top:0;z-index:2;display:flex;gap:10px;padding:10px 0;margin-bottom:4px;background:#f4f6f8;flex-wrap:wrap}}
input{{flex:1;min-width:220px;padding:10px 12px;border:1px solid #cfd6de;border-radius:6px;font:inherit}}
button,a{{border:0;border-radius:6px;background:#1769e0;color:#fff;padding:10px 13px;font-weight:650;text-decoration:none;cursor:pointer}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d8dde4}}
th,td{{padding:11px 12px;border-bottom:1px solid #e7eaee;text-align:left;vertical-align:middle}}
th{{font-size:12px;text-transform:uppercase;color:#475467;background:#f9fafb}}.name{{overflow-wrap:anywhere}}
.number{{width:52px;color:#667085}}.action{{width:110px}}.action a{{display:inline-block;padding:7px 10px}}
#toast{{display:none;position:fixed;right:18px;bottom:18px;background:#17202a;color:#fff;padding:11px 14px;border-radius:6px}}#empty{{display:none;text-align:center;color:#667085;padding:30px}}
@media(max-width:700px){{.top{{padding:20px 14px}}main{{margin:16px auto;padding:0 10px}}th:nth-child(1),td:nth-child(1){{display:none}}th,td{{padding:10px 8px}}}}
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
