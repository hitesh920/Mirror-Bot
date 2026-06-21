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
            "<tr class='row'>"
            f"<td class='cell number'>{index}</td>"
            "<td class='cell'><div class='file-main'>"
            "<span class='file-icon'>FILE</span>"
            f"<div class='file-name'><strong>{html.escape(item.name)}</strong><span>Google Drive direct link</span></div>"
            "</div></td>"
            "<td class='cell action'>"
            f"<a class='primary-link' href='{html.escape(item.url, quote=True)}' target='_blank' rel='noopener'>Download</a>"
            "</td></tr>"
        )
    clipboard_json = json.dumps("\n\n".join(clipboard)).replace("</", "<\\/")
    return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(manifest.name)}</title>
<style>
{TEMP_PAGE_CSS}
body{{background:var(--bg)}}
.appbar{{position:sticky;top:0;z-index:8;border-bottom:1px solid var(--line);background:color-mix(in srgb,var(--surface) 94%,transparent);backdrop-filter:blur(14px)}}
.appbar-inner{{max-width:1180px;margin:0 auto;padding:14px 18px;display:flex;align-items:center;justify-content:space-between;gap:14px}}
.brand{{display:grid;gap:5px;min-width:0}}.brand h1{{font-size:22px;margin:0;overflow-wrap:anywhere}}.brand p{{margin:0;color:var(--muted)}}
.meta-pills{{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}}.meta-pills span{{display:inline-flex;align-items:center;min-height:34px;border:1px solid var(--line);border-radius:999px;background:var(--surface-soft);padding:6px 10px;color:var(--muted);font-weight:760;white-space:nowrap}}
.shell{{max-width:1180px;margin:0 auto;padding:16px 18px 28px;display:grid;gap:12px}}
.toolbar,.files-card{{border:1px solid var(--line);border-radius:10px;background:var(--surface);box-shadow:var(--shadow)}}.toolbar{{padding:10px;display:flex;gap:10px;align-items:center}}.toolbar input{{flex:1;min-width:220px}}.toolbar button{{white-space:nowrap}}
.files-card{{overflow:hidden}}.table-wrap{{overflow:auto}}table{{border:0;border-radius:0;box-shadow:none;min-width:620px}}th,td{{padding:0;border-bottom:1px solid var(--line)}}th{{height:42px;padding:0 14px}}.row:hover{{background:var(--surface-soft)}}.cell{{padding:11px 14px}}.number{{width:56px;color:var(--muted)}}.action{{width:124px;text-align:right}}
.file-main{{display:flex;align-items:center;gap:12px;min-width:0}}.file-icon{{width:34px;height:34px;border:1px solid var(--line);border-radius:8px;background:var(--surface-soft);display:inline-flex;align-items:center;justify-content:center;color:var(--muted);font-size:10px;font-weight:900;letter-spacing:.03em;flex:0 0 auto}}.file-name{{display:grid;gap:2px;min-width:0}}.file-name strong{{overflow-wrap:anywhere}}.file-name span{{font-size:12px;color:var(--muted)}}
.primary-link{{display:inline-flex;align-items:center;justify-content:center;min-height:32px;border-radius:7px;background:var(--primary);color:#fff;padding:6px 10px;font-weight:760;text-decoration:none}}#empty{{display:none;border:1px solid var(--line);border-radius:10px;background:var(--surface);padding:38px;text-align:center;color:var(--muted)}}
@media(max-width:700px){{.appbar-inner{{display:grid;padding:12px}}.meta-pills{{justify-content:flex-start}}.shell{{padding:12px 10px 22px}}.toolbar{{display:grid}}.number,th:nth-child(1){{display:none}}table{{min-width:480px}}.cell{{padding:10px}}}}
</style></head><body>
<header class="appbar"><div class="appbar-inner"><div class="brand"><h1>{html.escape(manifest.name)}</h1><p>Temporary Google Drive share</p></div><div class="meta-pills"><span>{len(manifest.files)} files</span><span>{manifest.folder_count} folders</span><span id="timer">Expires in 5:00</span></div></div></header>
<main class="shell"><section class="toolbar"><input id="search" type="search" placeholder="Search files"><button id="copy">Copy All Files and Links</button></section>
<section class="files-card"><div class="table-wrap"><table><thead><tr><th>#</th><th>File name</th><th></th></tr></thead><tbody id="rows">{"".join(rows)}</tbody></table></div></section><div id="empty">No matching files</div></main><div id="toast">Copied to clipboard</div>
<script>
const copyText={clipboard_json},expires={timeout};
const toast=()=>{{const t=document.querySelector('#toast');t.style.display='block';setTimeout(()=>t.style.display='none',2200)}};
document.querySelector('#copy').onclick=async()=>{{try{{await navigator.clipboard.writeText(copyText)}}catch(e){{const x=document.createElement('textarea');x.value=copyText;document.body.appendChild(x);x.select();document.execCommand('copy');x.remove()}}toast()}};
document.querySelector('#search').oninput=e=>{{const q=e.target.value.toLowerCase();let shown=0;document.querySelectorAll('#rows tr').forEach(r=>{{r.hidden=!r.textContent.toLowerCase().includes(q);if(!r.hidden)shown++}});document.querySelector('#empty').style.display=shown?'none':'block'}};
let left=expires;setInterval(()=>{{left=Math.max(0,left-1);document.querySelector('#timer').textContent=`Expires in ${{Math.floor(left/60)}}:${{String(left%60).padStart(2,'0')}}`;if(!left){{window.close();document.body.innerHTML='<main><h2>Share page expired</h2></main>'}}}},1000);
</script></body></html>"""
