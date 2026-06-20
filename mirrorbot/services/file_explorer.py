import asyncio
import html
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
            if target.exists(): raise web.HTTPConflict(text="Destination already exists")
            target.mkdir()
            apply_media_permissions(self.root, target)
        elif action == "rename":
            source = self._path(data.get("source", ""))
            if source == self.root: raise web.HTTPForbidden(text="Cannot rename downloads root")
            target = self._path((source.parent.relative_to(self.root) / self._name(data.get("name", ""))).as_posix(), False)
            if target.exists(): raise web.HTTPConflict(text="Destination already exists")
            source.rename(target)
            apply_media_permissions(self.root, target)
        elif action in {"copy", "move"}:
            destination = self._path(data.get("destination", ""))
            if not destination.is_dir(): raise web.HTTPBadRequest(text="Destination is not a folder")
            sources = [self._path(relative) for relative in data.get("sources", [])]
            if not sources: raise web.HTTPBadRequest(text="Select at least one item")
            targets = [self._path((destination.relative_to(self.root) / source.name).as_posix(), False) for source in sources]
            if len(set(targets)) != len(targets):
                raise web.HTTPConflict(text="Selected items have duplicate destination names")
            for source, target in zip(sources, targets):
                if source == self.root: raise web.HTTPForbidden(text="Cannot copy or move downloads root")
                if source.is_dir() and (destination == source or source in destination.parents):
                    raise web.HTTPBadRequest(text=f"Cannot place {source.name} inside itself")
                if target.exists(): raise web.HTTPConflict(text=f"Destination already exists: {source.name}")
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
            if not targets: raise web.HTTPBadRequest(text="Select at least one item")
            if self.root in targets: raise web.HTTPForbidden(text="Cannot delete downloads root")
            for target in targets:
                shutil.rmtree(target) if target.is_dir() else target.unlink()
        elif action == "upload":
            paths = [self._path(relative) for relative in data.get("sources", [])]
            if not paths: raise web.HTTPBadRequest(text="Select at least one item")
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
body{background:linear-gradient(180deg,color-mix(in srgb,var(--surface-soft) 62%,var(--bg)),var(--bg) 260px)}
.explorer-shell{max-width:1180px;margin:18px auto 28px;padding:0 18px;display:grid;gap:14px}
.hero-panel,.action-panel,.files-panel{border:1px solid var(--line);border-radius:10px;background:var(--surface);box-shadow:var(--shadow)}
.hero-panel{padding:18px;display:flex;justify-content:space-between;gap:18px;align-items:flex-start}
.hero-copy{min-width:0}.hero-copy h1{font-size:24px;margin:0 0 8px}.hero-copy p{margin:0;color:var(--muted)}
.path-line{margin-top:14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.breadcrumbs{display:flex;gap:6px;align-items:center;flex-wrap:wrap;min-width:0;color:var(--muted)}
.breadcrumbs button{min-height:32px;padding:5px 9px;border-radius:999px;font-weight:700;background:var(--surface-soft)}
.breadcrumbs .sep{color:var(--line-strong)}
.hero-meta{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.hero-meta span,.selection-pill{display:inline-flex;align-items:center;min-height:30px;border:1px solid var(--line);border-radius:999px;background:var(--surface-soft);padding:5px 10px;color:var(--muted);font-weight:700}
.action-panel{padding:14px;display:grid;gap:12px}.action-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.action-group{border:1px solid var(--line);border-radius:9px;background:var(--surface-soft);padding:12px;min-width:0}.group-title{margin:0 0 10px;color:var(--muted);font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.04em}.button-row{display:flex;gap:8px;flex-wrap:wrap}.button-row button{min-height:36px}.button-row button:disabled{opacity:.45;cursor:not-allowed}.button-row button:disabled:hover{background:var(--surface)}
.files-panel{overflow:hidden}.files-head{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:13px 14px;border-bottom:1px solid var(--line);background:var(--surface-soft)}.files-head strong{font-size:15px}.files-head .muted{color:var(--muted)}
table{border:0;border-radius:0;box-shadow:none}th,td{padding:12px 14px}.check{width:44px}.link{width:122px;text-align:right}.name{cursor:pointer;color:var(--primary);font-weight:760}.parent .name{color:var(--text)}.kind,.size{color:var(--muted)}a.download{min-height:32px;padding:6px 10px}.empty{padding:38px}.parent-label{display:inline-flex;gap:8px;align-items:center}.parent-label::before{content:'..';display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border:1px solid var(--line);border-radius:7px;background:var(--surface-soft);font-weight:900;color:var(--text)}
@media(max-width:860px){.hero-panel{display:grid}.hero-meta{justify-content:flex-start}.action-grid{grid-template-columns:1fr}.button-row button{flex:1 1 auto}.kind,.size,th:nth-child(3),th:nth-child(4){display:none}.link{width:96px}.explorer-shell{margin:12px auto;padding:0 10px}th,td{padding:10px 9px}}
</style>
</head>
<body>
<header><div class="top"><h1>Local files</h1><div class="sub"><span>Temporary explorer</span><span id="timer">Expires in --:--</span></div></div></header>
<div class="explorer-shell">
  <section class="hero-panel">
    <div class="hero-copy">
      <h1>Downloads</h1>
      <p>Browse, organize, upload, and scan your media library.</p>
      <div class="path-line"><button id="parentBtn" type="button" onclick="up()">Parent folder</button><nav id="crumbs" class="breadcrumbs"></nav></div>
    </div>
    <div class="hero-meta"><span id="location">/downloads</span><span id="count">0 items</span><span id="selectedCount">0 selected</span></div>
  </section>
  <section class="action-panel">
    <div class="action-grid">
      <div class="action-group"><p class="group-title">File actions</p><div class="button-row"><button id="mkdirBtn" onclick="mkdir()">New folder</button><button id="renameBtn" onclick="renameOne()">Rename</button><button id="copyBtn" onclick="copyMove('copy')">Copy</button><button id="moveBtn" onclick="copyMove('move')">Move</button><button id="deleteBtn" class="danger" onclick="removeItems()">Delete</button></div></div>
      <div class="action-group"><p class="group-title">Upload selected</p><div class="button-row"><button id="uploadTelegramBtn" onclick="upload('telegram')">Telegram</button><button id="uploadDriveBtn" onclick="upload('google_drive')">Google Drive</button><button id="uploadBuzzBtn" onclick="upload('buzzheavier')">BuzzHeavier</button></div></div>
      <div class="action-group"><p class="group-title">Library</p><div class="button-row"><button class="primary" onclick="scan()">Scan Jellyfin</button></div></div>
    </div>
  </section>
  <section class="files-panel">
    <div class="files-head"><strong>Folder contents</strong><span class="muted" id="folderHint">Select files or folders to enable actions</span></div>
    <table><thead><tr><th class="check"><input type="checkbox" id="all" aria-label="Select all"></th><th>Name</th><th>Type</th><th>Size</th><th></th></tr></thead><tbody id="rows"></tbody></table>
  </section>
</div>
<div id="toast"></div>
<dialog id="extend"><h3>Keep this session open?</h3><p>This file explorer expires in under one minute.</p><button class="primary" onclick="extendSession()">Extend 5 minutes</button><button onclick="this.closest('dialog').close()">Not now</button></dialog>
<script>
const token=location.pathname.split('/').pop();let path='',expiresAt=0,asked=false;
const api=(action,data={})=>fetch(`/local/${token}/api/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}).then(async r=>{if(!r.ok)throw Error(await r.text());return r.json()});
const selected=()=>[...document.querySelectorAll('.pick:checked')].map(x=>x.value);
function toast(x){let t=document.querySelector('#toast');t.textContent=x;t.style.display='block';setTimeout(()=>t.style.display='none',2500)}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function enc(s){return encodeURIComponent(s).replace(/'/g,'%27')}
function currentLabel(){return '/downloads'+(path?'/'+path:'')}
function up(){if(path)load(path.split('/').slice(0,-1).join('/'))}
function renderBreadcrumbs(){const crumbs=document.querySelector('#crumbs');const parts=path?path.split('/').filter(Boolean):[];let acc='';let html=`<button type="button" onclick="load('')">downloads</button>`;for(const part of parts){acc=acc?`${acc}/${part}`:part;html+=`<span class="sep">/</span><button type="button" onclick="load(decodeURIComponent('${enc(acc)}'))">${esc(part)}</button>`}crumbs.innerHTML=html;document.querySelector('#parentBtn').disabled=!path}
function updateSelection(){const count=selected().length;document.querySelector('#selectedCount').textContent=`${count} selected`;document.querySelector('#folderHint').textContent=count?`${count} selected`:'Select files or folders to enable actions';document.querySelector('#renameBtn').disabled=count!==1;for(const id of ['copyBtn','moveBtn','deleteBtn','uploadTelegramBtn','uploadDriveBtn','uploadBuzzBtn'])document.querySelector('#'+id).disabled=count===0;const picks=[...document.querySelectorAll('.pick')];document.querySelector('#all').checked=picks.length>0&&picks.every(x=>x.checked)}
async function load(p=path){let r=await fetch(`/local/${token}/api/list?path=${encodeURIComponent(p)}`);if(!r.ok){document.body.innerHTML='<main><h2>Session expired</h2></main>';return}let d=await r.json();path=d.path;expiresAt=d.expiresAt;asked=false;const current=currentLabel();document.querySelector('#location').textContent=current;document.querySelector('#count').textContent=`${d.items.length} item${d.items.length===1?'':'s'}`;renderBreadcrumbs();const parent=path?`<tr class="parent"><td></td><td class="name" onclick="up()"><span class="parent-label">Parent folder</span></td><td class="kind">parent folder</td><td class="size">-</td><td></td></tr>`:'';const items=d.items.map(i=>`<tr><td><input class="pick" type="checkbox" value="${esc(i.path)}" onchange="updateSelection()"></td><td class="name" onclick="openItem(decodeURIComponent('${enc(i.path)}'),'${i.type}')">${esc(i.name)}</td><td class="kind">${i.type}</td><td class="size">${i.size}</td><td class="link">${i.type==='file'?`<a class="download" href="/local/${token}/download?path=${encodeURIComponent(i.path)}">Download</a>`:''}</td></tr>`).join('');document.querySelector('#rows').innerHTML=parent+(items||`<tr><td class="empty" colspan="5">This folder is empty</td></tr>`);document.querySelector('#all').checked=false;updateSelection()}
function openItem(p,t){if(t==='folder')load(p)}
async function mkdir(){let name=prompt('Folder name');if(name)await act('mkdir',{path,name})}
async function renameOne(){let s=selected();if(s.length!==1)return toast('Select one item');let name=prompt('New name',s[0].split('/').pop());if(name)await act('rename',{source:s[0],name})}
async function copyMove(a){let s=selected();if(!s.length)return toast('Select items');let destination=prompt('Destination path under downloads',path);if(destination!==null)await act(a,{sources:s,destination})}
async function removeItems(){let s=selected();if(!s.length)return toast('Select items');if(confirm('Permanently delete?\n'+s.join('\n')))await act('delete',{sources:s})}
function destinationLabel(destination){return destination==='telegram'?'Telegram':destination==='google_drive'?'Google Drive':'BuzzHeavier'}
async function upload(destination){let s=selected();if(!s.length)return toast('Select items');await act('upload',{sources:s,destination});toast(`${destinationLabel(destination)} upload tasks started`)}
async function scan(){await act('scan');toast('Jellyfin scan requested')}
async function act(a,d={}){try{await api(a,d);await load()}catch(e){toast(e.message)}}
async function extendSession(){let d=await api('extend');expiresAt=d.expiresAt;asked=false;document.querySelector('#extend').close();toast('Session extended')}
document.querySelector('#all').onchange=e=>{document.querySelectorAll('.pick').forEach(x=>x.checked=e.target.checked);updateSelection()};
setInterval(()=>{let n=Math.max(0,Math.ceil(expiresAt-Date.now()/1000));document.querySelector('#timer').textContent=`Expires in ${Math.floor(n/60)}:${String(n%60).padStart(2,'0')}`;if(n<=60&&!asked){asked=true;document.querySelector('#extend').showModal()}if(!n){window.close();document.body.innerHTML='<main><h2>Session expired</h2></main>'}},1000);load();
</script>
</body>
</html>'''.replace("{TEMP_PAGE_CSS}", TEMP_PAGE_CSS)
