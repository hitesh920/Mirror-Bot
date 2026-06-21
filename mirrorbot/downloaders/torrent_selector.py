import asyncio
import html
import logging
import secrets
from dataclasses import dataclass
from pathlib import PurePosixPath

from aiohttp import web

from ..services.page_style import TEMP_PAGE_CSS
from .qbittorrent import QBittorrentClient

LOGGER = logging.getLogger(__name__)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def build_tree(files: list[dict]) -> dict:
    root = {"folders": {}, "files": []}
    for file in files:
        node = root
        parts = PurePosixPath(file["name"]).parts
        for folder in parts[:-1]:
            node = node["folders"].setdefault(folder, {"folders": {}, "files": []})
        node["files"].append({**file, "label": parts[-1]})
    return root


def render_tree(node: dict, depth: int = 0) -> str:
    rows = []
    for folder_name, folder in sorted(
        node["folders"].items(), key=lambda item: item[0].lower()
    ):
        folder_id = secrets.token_hex(6)
        children = render_tree(folder, depth + 1)
        rows.append(
            "<li class='folder'>"
            f"<div class='row' style='--depth:{depth}'>"
            f"<button class='expand' type='button' aria-expanded='false' data-target='{folder_id}'>+</button>"
            "<input class='folder-check' type='checkbox'>"
            f"<button class='folder-name' type='button' data-target='{folder_id}'>{html.escape(folder_name)}</button>"
            "</div>"
            f"<ul id='{folder_id}' hidden>{children}</ul>"
            "</li>"
        )
    for file in sorted(node["files"], key=lambda item: item["label"].lower()):
        rows.append(
            "<li class='file'>"
            f"<label class='row' style='--depth:{depth}'>"
            f"<span class='spacer'></span><input class='file-check' type='checkbox' name='file' value='{file['index']}'>"
            f"<span class='name'>{html.escape(file['label'])}</span>"
            f"<small>{human_size(file.get('size', 0))}</small>"
            "</label></li>"
        )
    return "".join(rows)


@dataclass
class Selection:
    token: str
    torrent_hash: str
    files: list[dict]
    submitted: asyncio.Event
    closed: asyncio.Event
    cancelled: bool = False


class TorrentSelector:
    def __init__(
        self,
        qb: QBittorrentClient,
        public_base_url: str,
        port: int,
        timeout: int,
    ):
        self.qb = qb
        self.public_base_url = public_base_url.rstrip("/")
        self.port = port
        self.timeout = timeout
        self.lock = asyncio.Lock()
        self.selection: Selection | None = None
        self.runner: web.AppRunner | None = None

    async def select(self, torrent_hash: str, files: list[dict]) -> str:
        async with self.lock:
            token = secrets.token_urlsafe(32)
            selection = Selection(
                token,
                torrent_hash,
                files,
                asyncio.Event(),
                asyncio.Event(),
            )
            await self._start_server()
            self.selection = selection
            url = f"{self.public_base_url}/select/{token}"
            LOGGER.info("Torrent selector opened hash=%s", torrent_hash[:8])
            try:
                await asyncio.wait_for(
                    self.selection.submitted.wait(), timeout=self.timeout
                )
                if selection.cancelled:
                    raise asyncio.CancelledError()
                return url
            except TimeoutError as exc:
                raise TimeoutError("Torrent file selection timed out") from exc
            finally:
                await self._stop_server()
                selection.closed.set()
                self.selection = None
                LOGGER.info("Torrent selector closed hash=%s", torrent_hash[:8])

    async def cancel(self, torrent_hash: str) -> None:
        selection = self.selection
        if selection and selection.torrent_hash == torrent_hash:
            selection.cancelled = True
            selection.submitted.set()
            try:
                await asyncio.wait_for(selection.closed.wait(), timeout=5)
            except TimeoutError:
                await self._stop_server()

    async def cancel_all(self) -> None:
        selection = self.selection
        if selection:
            await self.cancel(selection.torrent_hash)

    async def _start_server(self) -> None:
        app = web.Application()
        app.router.add_get("/select/{token}", self._show)
        app.router.add_post("/select/{token}", self._submit)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "0.0.0.0", self.port).start()

    async def _stop_server(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    def _valid(self, request: web.Request) -> bool:
        return bool(
            self.selection
            and secrets.compare_digest(
                request.match_info.get("token", ""), self.selection.token
            )
        )

    async def _show(self, request: web.Request) -> web.Response:
        if not self._valid(request):
            raise web.HTTPNotFound()
        rows = render_tree(build_tree(self.selection.files))
        page = f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Select torrent files</title>
<style>
{TEMP_PAGE_CSS}
body{{background:var(--bg)}}
.appbar{{position:sticky;top:0;z-index:8;border-bottom:1px solid var(--line);background:color-mix(in srgb,var(--surface) 94%,transparent);backdrop-filter:blur(14px)}}
.appbar-inner{{max-width:1180px;margin:0 auto;padding:14px 18px;display:flex;align-items:center;justify-content:space-between;gap:14px}}
.brand{{display:grid;gap:5px;min-width:0}}.brand h1{{font-size:22px;margin:0}}.brand p{{margin:0;color:var(--muted)}}
.meta-pills{{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}}.meta-pills span{{display:inline-flex;align-items:center;min-height:34px;border:1px solid var(--line);border-radius:999px;background:var(--surface-soft);padding:6px 10px;color:var(--muted);font-weight:760;white-space:nowrap}}
.shell{{max-width:1180px;margin:0 auto;padding:16px 18px 88px;display:grid;gap:12px}}
.toolbar,.tree-card,.selectionbar{{border:1px solid var(--line);border-radius:10px;background:var(--surface);box-shadow:var(--shadow)}}
.toolbar{{padding:10px;display:flex;align-items:center;gap:10px}}.toolbar input{{flex:1;min-width:220px}}.toolbar-actions{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
ul{{list-style:none;margin:0;padding:0}}.tree-card{{overflow:hidden}}
.row{{display:grid;grid-template-columns:32px 24px minmax(0,1fr) auto;gap:9px;padding:10px 14px 10px calc(14px + var(--depth) * 20px);border-bottom:1px solid var(--line);align-items:center;transition:background .12s ease}}.row:hover{{background:var(--surface-soft)}}
.folder>.row{{background:color-mix(in srgb,var(--surface-soft) 45%,var(--surface))}}.folder-name,.name{{min-width:0;overflow-wrap:anywhere}}.folder-name{{justify-content:flex-start;min-height:0;padding:0;text-align:left;background:transparent;color:var(--text);font-weight:820;border:0;border-radius:2px}}.folder-name:hover{{background:transparent;color:var(--primary);text-decoration:underline}}
.name{{font-weight:760}}small{{color:var(--muted);white-space:nowrap}}.expand{{width:30px;height:30px;min-height:30px;padding:0;margin:0;background:var(--surface-soft);color:var(--text);border:1px solid var(--line-strong);border-radius:7px;font-weight:900}}.spacer{{width:30px}}
.selectionbar{{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:10;width:min(1180px,calc(100vw - 32px));padding:10px;display:flex;align-items:center;justify-content:space-between;gap:10px;border-color:color-mix(in srgb,var(--primary) 42%,var(--line));background:color-mix(in srgb,var(--primary-soft) 45%,var(--surface));backdrop-filter:blur(14px)}}.selectionbar .count{{font-weight:850}}.selection-actions{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}}
@media(max-width:760px){{.appbar-inner{{display:grid;padding:12px}}.meta-pills{{justify-content:flex-start}}.shell{{padding:12px 10px 92px}}.toolbar{{display:grid}}.toolbar-actions{{width:100%}}.toolbar-actions button{{flex:1 1 auto}}.row{{grid-template-columns:30px 22px minmax(0,1fr);padding-left:calc(10px + var(--depth) * 13px)}}small{{display:none}}.selectionbar{{display:grid;bottom:10px;width:calc(100vw - 20px)}}.selection-actions button{{flex:1 1 auto}}}}
</style></head><body>
<header class="appbar"><div class="appbar-inner"><div class="brand"><h1>Select torrent files</h1><p>Expand folders and choose only the files you want.</p></div><div class="meta-pills"><span>Nothing selected by default</span><span>Temporary selector</span></div></div></header>
<main class="shell">
<form method="post">
<section class="toolbar"><input id="search" type="search" placeholder="Search files and folders"><div class="toolbar-actions"><button class="secondary" type="button" id="check-all">Check all</button><button class="secondary" type="button" id="uncheck-all">Uncheck all</button></div></section>
<section class="tree-card"><ul id="tree">{rows}</ul></section>
<section class="selectionbar"><span class="count" id="count">0 files selected</span><div class="selection-actions"><button type="submit">Start download</button><button class="cancel" type="submit" name="action" value="cancel">Cancel</button></div></section>
</form>
</main>
<script>
const setChildren=(folder,checked)=>folder.querySelectorAll('input[type=checkbox]').forEach(box=>{{box.checked=checked;box.indeterminate=false;}});
const updateCount=()=>{{const n=document.querySelectorAll('.file-check:checked').length;document.getElementById('count').textContent=`${{n}} file${{n===1?'':'s'}} selected`;}};
const updateParents=element=>{{
 let folder=element.closest('.folder');
 while(folder){{
  const parent=folder.querySelector(':scope > .row > .folder-check');
  const files=[...folder.querySelectorAll('.file-check')];
  parent.checked=files.length>0&&files.every(file=>file.checked);
  parent.indeterminate=files.some(file=>file.checked)&&!parent.checked;
  folder=folder.parentElement.closest('.folder');
 }} updateCount();
}};
const toggleFolder=target=>{{
 const tree=document.getElementById(target); tree.hidden=!tree.hidden;
 const button=document.querySelector(`.expand[data-target="${{target}}"]`);
 button.textContent=tree.hidden?'+':'-'; button.setAttribute('aria-expanded',String(!tree.hidden));
}};
document.querySelectorAll('.expand,.folder-name').forEach(button=>button.addEventListener('click',()=>toggleFolder(button.dataset.target)));
document.querySelectorAll('.folder-check').forEach(box=>box.addEventListener('change',()=>{{setChildren(box.closest('.folder'),box.checked);updateParents(box.closest('.folder').parentElement);}}));
document.querySelectorAll('.file-check').forEach(box=>box.addEventListener('change',()=>{{updateParents(box);}}));
document.getElementById('check-all').addEventListener('click',()=>{{document.querySelectorAll('input[type=checkbox]').forEach(box=>{{box.checked=true;box.indeterminate=false;}});updateCount();}});
document.getElementById('uncheck-all').addEventListener('click',()=>{{document.querySelectorAll('input[type=checkbox]').forEach(box=>{{box.checked=false;box.indeterminate=false;}});updateCount();}});
document.getElementById('search').addEventListener('input',e=>{{const q=e.target.value.toLowerCase();document.querySelectorAll('#tree li.file').forEach(row=>row.hidden=!!q&&!row.textContent.toLowerCase().includes(q));document.querySelectorAll('#tree li.folder').forEach(row=>{{const match=!q||row.textContent.toLowerCase().includes(q);row.hidden=!match;if(q&&match){{const tree=row.querySelector(':scope > ul');if(tree)tree.hidden=false;const button=row.querySelector(':scope > .row > .expand');if(button){{button.textContent='-';button.setAttribute('aria-expanded','true')}}}}}});}});
</script>
</body></html>"""
        return web.Response(text=page, content_type="text/html")

    async def _submit(self, request: web.Request) -> web.Response:
        if not self._valid(request):
            raise web.HTTPNotFound()
        form = await request.post()
        if form.get("action") == "cancel":
            self.selection.cancelled = True
            self.selection.submitted.set()
            return web.Response(
                text="Torrent cancelled. You can close this page.",
                content_type="text/plain",
            )
        all_ids = {file["index"] for file in self.selection.files}
        selected = {
            int(value) for value in form.getall("file", []) if value.isdecimal()
        } & all_ids
        if not selected:
            return web.Response(
                text="Select at least one file.", status=400, content_type="text/plain"
            )
        skipped = [file_id for file_id in all_ids if file_id not in selected]
        await self.qb.set_file_priority(self.selection.torrent_hash, skipped, 0)
        await self.qb.set_file_priority(
            self.selection.torrent_hash, sorted(selected), 1
        )
        await self.qb.start(self.selection.torrent_hash)
        self.selection.submitted.set()
        return web.Response(
            text="Selection saved. You can close this page.",
            content_type="text/plain",
        )
