import asyncio
import logging
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from .media_library import apply_media_permissions
from .page_style import TEMP_PAGE_CSS
from .status import human_size
from .transfer_guard import ensure_disk_space
from ..downloaders.process import path_size

LOGGER = logging.getLogger(__name__)


@dataclass
class ExplorerSession:
    token: str
    chat_id: int
    expires_at: float


class FileExplorer:
    def __init__(self, root: Path, public_url: str, upload_callback, scan_callback, port: int = 8003):
        self.root = root.resolve()
        self.public_url = public_url.rstrip("/")
        self.upload_callback = upload_callback
        self.scan_callback = scan_callback
        self.port = port
        self.sessions: dict[str, ExplorerSession] = {}
        self.runner: web.AppRunner | None = None
        self.expiry_job: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    async def create(self, chat_id: int) -> str:
        async with self.lock:
            await self._start()
            token = secrets.token_urlsafe(32)
            self.sessions[token] = ExplorerSession(token, chat_id, time.time() + 300)
            LOGGER.info("File explorer session opened token=%s chat=%s", token[:8], chat_id)
            return f"{self.public_url}/local/{token}"

    async def close_all(self) -> None:
        async with self.lock:
            self.sessions.clear()
            await self._stop()

    async def _start(self) -> None:
        if self.runner:
            return
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_get("/local/{token}", self._page)
        app.router.add_get("/local/{token}/api/list", self._list)
        app.router.add_get("/local/{token}/download", self._download)
        app.router.add_post("/local/{token}/api/{action}", self._action)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "0.0.0.0", self.port).start()
        self.expiry_job = asyncio.create_task(self._expiry_loop())

    async def _stop(self) -> None:
        if self.expiry_job and self.expiry_job is not asyncio.current_task():
            self.expiry_job.cancel()
        self.expiry_job = None
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    async def _expiry_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(5)
                async with self.lock:
                    now = time.time()
                    expired = [token for token, session in self.sessions.items() if session.expires_at <= now]
                    for token in expired:
                        self.sessions.pop(token, None)
                        LOGGER.info("File explorer session expired token=%s", token[:8])
                    if not self.sessions:
                        await self._stop()
                        return
        except asyncio.CancelledError:
            pass

    def _session(self, request: web.Request) -> ExplorerSession:
        token = request.match_info.get("token", "")
        session = self.sessions.get(token)
        if session is None or not secrets.compare_digest(token, session.token) or session.expires_at <= time.time():
            raise web.HTTPGone(text="File explorer session expired")
        return session

    def _path(self, relative: str, must_exist: bool = True) -> Path:
        if relative.startswith(("/", "\\")):
            raise web.HTTPBadRequest(text="Absolute paths are not allowed")
        target = (self.root / relative).resolve(strict=False)
        if target != self.root and self.root not in target.parents:
            raise web.HTTPForbidden(text="Path is outside downloads")
        current = self.root
        for part in Path(relative).parts:
            current /= part
            if current.exists() and current.is_symlink():
                raise web.HTTPForbidden(text="Symbolic links are not allowed")
        if must_exist and not target.exists():
            raise web.HTTPNotFound(text="Path does not exist")
        return target

    @staticmethod
    def _name(value: str) -> str:
        value = value.strip()
        if not value or value in {".", ".."} or Path(value).name != value:
            raise web.HTTPBadRequest(text="Invalid name")
        return value

    async def _page(self, request: web.Request) -> web.Response:
        self._session(request)
        return web.Response(text=PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def _list(self, request: web.Request) -> web.Response:
        session = self._session(request)
        relative = request.query.get("path", "")
        folder = self._path(relative)
        if not folder.is_dir():
            raise web.HTTPBadRequest(text="Not a folder")
        items = []
        for item in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
            if item.is_symlink():
                continue
            stat = item.stat()
            items.append({
                "name": item.name,
                "path": item.relative_to(self.root).as_posix(),
                "type": "folder" if item.is_dir() else "file",
                "size": human_size(stat.st_size) if item.is_file() else "-",
            })
        return web.json_response({"path": relative, "items": items, "expiresAt": session.expires_at})

    async def _download(self, request: web.Request) -> web.StreamResponse:
        session = self._session(request)
        target = self._path(request.query.get("path", ""))
        if not target.is_file():
            raise web.HTTPBadRequest(text="Only files can be downloaded")
        LOGGER.info("File explorer download token=%s path=%s", session.token[:8], target)
        return web.FileResponse(target)

    def _schedule_scan(self, token: str, reason: str) -> None:
        async def run_scan() -> None:
            try:
                await self.scan_callback()
                LOGGER.info(
                    "File explorer background scan complete token=%s reason=%s",
                    token[:8],
                    reason,
                )
            except Exception:
                LOGGER.exception(
                    "File explorer background scan failed token=%s reason=%s",
                    token[:8],
                    reason,
                )

        asyncio.create_task(run_scan())

    async def _action(self, request: web.Request) -> web.Response:
        session = self._session(request)
        action = request.match_info["action"]
        data = await request.json()
        if action == "extend":
            session.expires_at = time.time() + 300
            LOGGER.info("File explorer session extended token=%s", session.token[:8])
            return web.json_response({"ok": True, "expiresAt": session.expires_at})
        if action == "scan":
            await self.scan_callback()
            LOGGER.info("File explorer requested Jellyfin scan token=%s", session.token[:8])
            return web.json_response({"ok": True})
        if action == "mkdir":
            parent = self._path(data.get("path", ""))
            target = self._path((parent.relative_to(self.root) / self._name(data.get("name", ""))).as_posix(), False)
            if target.exists():
                raise web.HTTPConflict(text="Destination already exists")
            target.mkdir()
            apply_media_permissions(self.root, target)
        elif action == "rename":
            source = self._path(data.get("source", ""))
            if source == self.root:
                raise web.HTTPForbidden(text="Cannot rename downloads root")
            target = self._path((source.parent.relative_to(self.root) / self._name(data.get("name", ""))).as_posix(), False)
            if target.exists():
                raise web.HTTPConflict(text="Destination already exists")
            source.rename(target)
            apply_media_permissions(self.root, target)
        elif action in {"copy", "move"}:
            destination = self._path(data.get("destination", ""))
            if not destination.is_dir():
                raise web.HTTPBadRequest(text="Destination is not a folder")
            sources = [self._path(relative) for relative in data.get("sources", [])]
            if not sources:
                raise web.HTTPBadRequest(text="Select at least one item")
            targets = [self._path((destination.relative_to(self.root) / source.name).as_posix(), False) for source in sources]
            if len(set(targets)) != len(targets):
                raise web.HTTPConflict(text="Selected items have duplicate destination names")
            for source, target in zip(sources, targets):
                if source == self.root:
                    raise web.HTTPForbidden(text="Cannot copy or move downloads root")
                if source.is_dir() and (destination == source or source in destination.parents):
                    raise web.HTTPBadRequest(text=f"Cannot place {source.name} inside itself")
                if target.exists():
                    raise web.HTTPConflict(text=f"Destination already exists: {source.name}")
            if action == "copy":
                ensure_disk_space(destination, sum(path_size(source) for source in sources))
            for source, target in zip(sources, targets):
                if action == "copy":
                    shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)
                else:
                    shutil.move(str(source), str(target))
                apply_media_permissions(self.root, target)
        elif action == "delete":
            targets = [self._path(relative) for relative in data.get("sources", [])]
            if not targets:
                raise web.HTTPBadRequest(text="Select at least one item")
            if self.root in targets:
                raise web.HTTPForbidden(text="Cannot delete downloads root")
            for target in targets:
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            self._schedule_scan(session.token, "delete")
        elif action == "upload":
            paths = [self._path(relative) for relative in data.get("sources", [])]
            if not paths:
                raise web.HTTPBadRequest(text="Select at least one item")
            destination = data.get("destination", "")
            if destination not in {"telegram", "google_drive", "buzzheavier"}:
                raise web.HTTPBadRequest(text="Invalid upload destination")
            await self.upload_callback(session.chat_id, paths, destination)
        else:
            raise web.HTTPBadRequest(text="Unknown action")
        LOGGER.info("File explorer action=%s token=%s", action, session.token[:8])
        return web.json_response({"ok": True})


PAGE = r'''<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Local files</title>
<style>
{TEMP_PAGE_CSS}
body{background:var(--bg)}
.explorer{max-width:1180px;margin:0 auto;padding:16px 18px 28px;display:grid;gap:12px}
.appbar{position:sticky;top:0;z-index:8;border-bottom:1px solid var(--line);background:color-mix(in srgb,var(--surface) 94%,transparent);backdrop-filter:blur(14px)}
.appbar-inner{max-width:1180px;margin:0 auto;padding:14px 18px;display:flex;align-items:center;justify-content:space-between;gap:14px}
.brand{display:grid;gap:5px;min-width:0}.brand h1{font-size:22px;margin:0}.crumbs{display:flex;align-items:center;gap:6px;min-width:0;flex-wrap:wrap;color:var(--muted)}
.crumbs button{min-height:28px;border:0;background:transparent;padding:2px 4px;border-radius:6px;color:var(--primary);font-weight:760}.crumbs button:hover{background:var(--primary-soft)}.crumbs .sep{color:var(--line-strong)}
.header-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}.timer{display:inline-flex;align-items:center;min-height:34px;border:1px solid var(--line);border-radius:999px;background:var(--surface-soft);padding:6px 10px;color:var(--muted);font-weight:760;white-space:nowrap}
.toolbar,.selectionbar,.files-card{border:1px solid var(--line);border-radius:10px;background:var(--surface);box-shadow:var(--shadow)}
.toolbar{padding:10px;display:flex;align-items:center;justify-content:space-between;gap:10px}.toolbar.hidden{display:none}.toolbar-left,.toolbar-right{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.toolbar .hint{color:var(--muted);font-weight:700}
.selectionbar{display:none;position:sticky;top:69px;z-index:7;padding:10px;align-items:center;justify-content:space-between;gap:10px;border-color:color-mix(in srgb,var(--primary) 42%,var(--line));background:color-mix(in srgb,var(--primary-soft) 45%,var(--surface))}.selectionbar.active{display:flex}.selection-title{font-weight:850}.selection-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}.upload-wrap{display:flex;gap:6px;align-items:center}select{min-height:38px;border:1px solid var(--line-strong);border-radius:7px;background:var(--surface);color:var(--text);padding:8px 10px;font:inherit;font-weight:760}
.files-card{overflow:hidden}.table-wrap{overflow:auto}table{border:0;border-radius:0;box-shadow:none;min-width:680px}th,td{padding:0;border-bottom:1px solid var(--line)}th{height:42px;padding:0 14px}.row{transition:background .12s ease}.row:hover{background:var(--surface-soft)}.row.selected{background:color-mix(in srgb,var(--primary-soft) 58%,var(--surface))}.cell{padding:11px 14px}.check{width:48px}.type,.size{color:var(--muted);white-space:nowrap}.actions{width:120px;text-align:right}.file-main{display:flex;align-items:center;gap:12px;min-width:0}.file-icon{width:34px;height:34px;border:1px solid var(--line);border-radius:8px;background:var(--surface-soft);display:inline-flex;align-items:center;justify-content:center;color:var(--muted);font-size:10px;font-weight:900;letter-spacing:.03em;flex:0 0 auto}.file-icon.folder{background:var(--primary-soft);color:var(--primary);border-color:color-mix(in srgb,var(--primary) 25%,var(--line))}.file-icon.folder::before{content:'DIR'}.file-icon.file::before{content:'FILE'}.file-name{min-width:0}.file-name button{justify-content:flex-start;min-height:0;border:0;background:transparent;padding:0;color:var(--primary);font-weight:820;text-align:left;overflow-wrap:anywhere}.file-name button:hover{text-decoration:underline;background:transparent}.file-sub{margin-top:2px;color:var(--muted);font-size:12px}.parent-row .file-name button{color:var(--text)}.parent-row .file-icon::before{content:'..'}
a.download{min-height:32px;padding:6px 10px}.empty-state{padding:46px 16px;text-align:center;color:var(--muted)}.empty-state strong{display:block;color:var(--text);font-size:16px;margin-bottom:4px}
button.iconish{min-width:38px;padding-inline:9px}.muted-button{color:var(--muted)}button:disabled,select:disabled{opacity:.45;cursor:not-allowed}button:disabled:hover{background:var(--surface)}
@media(max-width:760px){.appbar-inner{display:grid;padding:12px}.header-actions{justify-content:flex-start}.explorer{padding:12px 10px 22px}.toolbar,.selectionbar{align-items:stretch}.toolbar{display:grid}.toolbar-left,.toolbar-right,.selection-actions{width:100%}.toolbar-left button,.toolbar-right button,.selection-actions button{flex:1 1 auto}.selectionbar{top:82px;display:none}.selectionbar.active{display:grid}.upload-wrap{width:100%;display:grid;grid-template-columns:1fr auto}.type,th:nth-child(3){display:none}.actions{width:92px}.cell{padding:10px}.check{width:42px}table{min-width:520px}.brand h1{font-size:20px}}
</style>
</head>
<body>
<header class="appbar">
  <div class="appbar-inner">
    <div class="brand">
      <h1>Downloads</h1>
      <nav id="crumbs" class="crumbs" aria-label="Breadcrumbs"></nav>
    </div>
    <div class="header-actions">
      <span id="timer" class="timer">Expires in --:--</span>
      <button class="secondary" type="button" onclick="scan()">Scan Jellyfin</button>
    </div>
  </div>
</header>
<main class="explorer">
  <section id="defaultTools" class="toolbar">
    <div class="toolbar-left">
      <button type="button" onclick="mkdir()">New folder</button>
      <button type="button" onclick="load(path)">Refresh</button>
      <button id="parentBtn" class="muted-button" type="button" onclick="up()">Parent folder</button>
    </div>
    <div class="toolbar-right"><span id="count" class="hint">0 items</span></div>
  </section>
  <section id="selectionBar" class="selectionbar" aria-live="polite">
    <div class="selection-title" id="selectedCount">0 selected</div>
    <div class="selection-actions">
      <button id="renameBtn" type="button" onclick="renameOne()">Rename</button>
      <button type="button" onclick="copyMove('copy')">Copy</button>
      <button type="button" onclick="copyMove('move')">Move</button>
      <button class="danger" type="button" onclick="removeItems()">Delete</button>
      <div class="upload-wrap">
        <select id="uploadDest" aria-label="Upload destination">
          <option value="telegram">Telegram</option>
          <option value="google_drive">Google Drive</option>
          <option value="buzzheavier">BuzzHeavier</option>
        </select>
        <button class="primary" type="button" onclick="uploadSelected()">Upload</button>
      </div>
      <button class="secondary" type="button" onclick="clearSelection()">Clear</button>
    </div>
  </section>
  <section class="files-card">
    <div class="table-wrap">
      <table>
        <thead><tr><th class="check"><input type="checkbox" id="all" aria-label="Select all"></th><th>Name</th><th>Type</th><th>Size</th><th></th></tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </section>
</main>
<div id="toast"></div>
<dialog id="extend"><h3>Keep this session open?</h3><p>This file explorer expires in under one minute.</p><button class="primary" onclick="extendSession()">Extend 5 minutes</button><button onclick="this.closest('dialog').close()">Not now</button></dialog>
<script>
const token=location.pathname.split('/').pop();let path='',expiresAt=0,asked=false;
const api=(action,data={})=>fetch(`/local/${token}/api/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}).then(async r=>{if(!r.ok)throw Error(await r.text());return r.json()});
const selected=()=>[...document.querySelectorAll('.pick:checked')].map(x=>x.value);
function toast(x){let t=document.querySelector('#toast');t.textContent=x;t.style.display='block';setTimeout(()=>t.style.display='none',2500)}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function enc(s){return encodeURIComponent(s).replace(/'/g,'%27')}
function up(){if(path)load(path.split('/').slice(0,-1).join('/'))}
function renderBreadcrumbs(){const crumbs=document.querySelector('#crumbs');const parts=path?path.split('/').filter(Boolean):[];let acc='';let html=`<button type="button" onclick="load('')">Downloads</button>`;for(const part of parts){acc=acc?`${acc}/${part}`:part;html+=`<span class="sep">/</span><button type="button" onclick="load(decodeURIComponent('${enc(acc)}'))">${esc(part)}</button>`}crumbs.innerHTML=html;document.querySelector('#parentBtn').disabled=!path}
function clearSelection(){document.querySelectorAll('.pick').forEach(x=>x.checked=false);updateSelection()}
function updateSelection(){const count=selected().length;document.querySelector('#selectedCount').textContent=`${count} selected`;document.querySelector('#selectionBar').classList.toggle('active',count>0);document.querySelector('#defaultTools').classList.toggle('hidden',count>0);document.querySelector('#renameBtn').disabled=count!==1;const picks=[...document.querySelectorAll('.pick')];document.querySelector('#all').checked=picks.length>0&&picks.every(x=>x.checked);document.querySelectorAll('tr.row').forEach(row=>{const pick=row.querySelector('.pick');row.classList.toggle('selected',!!pick&&pick.checked)})}
async function load(p=path){let r=await fetch(`/local/${token}/api/list?path=${encodeURIComponent(p)}&_=${Date.now()}`,{cache:'no-store'});if(!r.ok){document.body.innerHTML='<main><h2>Session expired</h2></main>';return}let d=await r.json();path=d.path;expiresAt=d.expiresAt;asked=false;document.querySelector('#count').textContent=`${d.items.length} item${d.items.length===1?'':'s'}`;renderBreadcrumbs();const parent=path?`<tr class="row parent-row"><td class="cell check"></td><td class="cell"><div class="file-main"><span class="file-icon folder"></span><div class="file-name"><button type="button" onclick="up()">Parent folder</button><div class="file-sub">Go up one level</div></div></div></td><td class="cell type">Folder</td><td class="cell size">-</td><td class="cell actions"></td></tr>`:'';const items=d.items.map(i=>`<tr class="row"><td class="cell check"><input class="pick" type="checkbox" value="${esc(i.path)}" onchange="updateSelection()"></td><td class="cell"><div class="file-main"><span class="file-icon ${i.type==='folder'?'folder':'file'}"></span><div class="file-name"><button type="button" onclick="openItem(decodeURIComponent('${enc(i.path)}'),'${i.type}')">${esc(i.name)}</button><div class="file-sub">${esc(i.path)}</div></div></div></td><td class="cell type">${i.type}</td><td class="cell size">${i.size}</td><td class="cell actions">${i.type==='file'?`<a class="download" href="/local/${token}/download?path=${encodeURIComponent(i.path)}">Download</a>`:''}</td></tr>`).join('');document.querySelector('#rows').innerHTML=parent+(items||`<tr><td class="empty-state" colspan="5"><strong>This folder is empty</strong>Create a folder or move files here.</td></tr>`);document.querySelector('#all').checked=false;updateSelection()}
function openItem(p,t){if(t==='folder')load(p)}
async function mkdir(){let name=prompt('Folder name');if(name)await act('mkdir',{path,name})}
async function renameOne(){let s=selected();if(s.length!==1)return toast('Select one item');let name=prompt('New name',s[0].split('/').pop());if(name)await act('rename',{source:s[0],name})}
async function copyMove(a){let s=selected();if(!s.length)return toast('Select items');let destination=prompt('Destination path under downloads',path);if(destination!==null)await act(a,{sources:s,destination})}
async function removeItems(){let s=selected();if(!s.length)return toast('Select items');if(confirm('Permanently delete?\n'+s.join('\n'))){toast('Deleting...');await act('delete',{sources:s})}}
function destinationLabel(destination){return destination==='telegram'?'Telegram':destination==='google_drive'?'Google Drive':'BuzzHeavier'}
async function uploadSelected(){let destination=document.querySelector('#uploadDest').value;let s=selected();if(!s.length)return toast('Select items');await act('upload',{sources:s,destination});toast(`${destinationLabel(destination)} upload tasks started`)}
async function scan(){await act('scan');toast('Jellyfin scan requested')}
async function act(a,d={}){try{await api(a,d);clearSelection();await load(path);if(a==='delete')toast('Deleted')}catch(e){toast(e.message)}}
async function extendSession(){let d=await api('extend');expiresAt=d.expiresAt;asked=false;document.querySelector('#extend').close();toast('Session extended')}
document.querySelector('#all').onchange=e=>{document.querySelectorAll('.pick').forEach(x=>x.checked=e.target.checked);updateSelection()};
setInterval(()=>{let n=Math.max(0,Math.ceil(expiresAt-Date.now()/1000));document.querySelector('#timer').textContent=`Expires in ${Math.floor(n/60)}:${String(n%60).padStart(2,'0')}`;if(n<=60&&!asked){asked=true;document.querySelector('#extend').showModal()}if(!n){window.close();document.body.innerHTML='<main><h2>Session expired</h2></main>'}},1000);load();
</script>
</body>
</html>'''.replace("{TEMP_PAGE_CSS}", TEMP_PAGE_CSS)
