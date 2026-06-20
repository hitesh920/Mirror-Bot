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


PAGE = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Local files</title>
<style>
{TEMP_PAGE_CSS}
.bar{padding:10px 18px}
.actions{max-width:1180px;margin:auto;display:flex;gap:8px;flex-wrap:wrap}
.crumbs{margin:0 0 10px;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;overflow-wrap:anywhere}
.name{cursor:pointer;color:var(--primary);font-weight:760}
.parent .name{color:var(--text)}
.kind,.size{color:var(--muted)}
.check{width:44px}.link{width:116px}
a.download{min-height:34px;padding:7px 10px}
@media(max-width:700px){.bar{padding:8px 10px}.actions{gap:5px}.kind,.size,th:nth-child(3),th:nth-child(4){display:none}.link{width:92px}}
</style></head><body>
<header><div class="top"><h1>Local files</h1><div class="sub"><span id="location">/downloads</span><span id="count">0 items</span><span id="timer"></span></div></div></header>
<div class="bar"><div class="actions"><button onclick="mkdir()">New folder</button><button onclick="renameOne()">Rename</button><button onclick="copyMove('copy')">Copy</button><button onclick="copyMove('move')">Move</button><button class="danger" onclick="removeItems()">Delete</button><button onclick="upload('telegram')">Upload to Telegram</button><button onclick="upload('google_drive')">Upload to Google Drive</button><button onclick="upload('buzzheavier')">Upload to BuzzHeavier</button><button class="primary" onclick="scan()">Scan Jellyfin</button></div></div>
<main><div id="crumbs" class="crumbs"></div><table><thead><tr><th class="check"><input type="checkbox" id="all"></th><th>Name</th><th>Type</th><th>Size</th><th></th></tr></thead><tbody id="rows"></tbody></table></main><div id="toast"></div>
<dialog id="extend"><h3>Keep this session open?</h3><p>This file explorer expires in under one minute.</p><button class="primary" onclick="extendSession()">Extend 5 minutes</button><button onclick="this.closest('dialog').close()">Not now</button></dialog>
<script>
const token=location.pathname.split('/').pop();let path='',expiresAt=0,asked=false;
const api=(action,data={})=>fetch(`/local/${token}/api/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}).then(async r=>{if(!r.ok)throw Error(await r.text());return r.json()});
const selected=()=>[...document.querySelectorAll('.pick:checked')].map(x=>x.value);
function toast(x){let t=document.querySelector('#toast');t.textContent=x;t.style.display='block';setTimeout(()=>t.style.display='none',2500)}
function esc(s){return s.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function enc(s){return encodeURIComponent(s).replace(/'/g,'%27')}
function up(){load(path.split('/').slice(0,-1).join('/'))}
async function load(p=path){let r=await fetch(`/local/${token}/api/list?path=${encodeURIComponent(p)}`);if(!r.ok){document.body.innerHTML='<main><h2>Session expired</h2></main>';return}let d=await r.json();path=d.path;expiresAt=d.expiresAt;asked=false;const current='/downloads'+(path?'/'+path:'');document.querySelector('#crumbs').textContent=current;document.querySelector('#location').textContent=current;document.querySelector('#count').textContent=`${d.items.length} item${d.items.length===1?'':'s'}`;const parent=path?`<tr class="parent"><td></td><td class="name" onclick="up()">..</td><td class="kind">parent folder</td><td class="size">-</td><td></td></tr>`:'';const items=d.items.map(i=>`<tr><td><input class="pick" type="checkbox" value="${esc(i.path)}"></td><td class="name" onclick="openItem(decodeURIComponent('${enc(i.path)}'),'${i.type}')">${esc(i.name)}</td><td class="kind">${i.type}</td><td class="size">${i.size}</td><td class="link">${i.type==='file'?`<a class="download" href="/local/${token}/download?path=${encodeURIComponent(i.path)}">Download</a>`:''}</td></tr>`).join('');document.querySelector('#rows').innerHTML=parent+(items||`<tr><td class="empty" colspan="5">This folder is empty</td></tr>`);document.querySelector('#all').checked=false}
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
document.querySelector('#all').onchange=e=>document.querySelectorAll('.pick').forEach(x=>x.checked=e.target.checked);
setInterval(()=>{let n=Math.max(0,Math.ceil(expiresAt-Date.now()/1000));document.querySelector('#timer').textContent=`Expires in ${Math.floor(n/60)}:${String(n%60).padStart(2,'0')}`;if(n<=60&&!asked){asked=true;document.querySelector('#extend').showModal()}if(!n){window.close();document.body.innerHTML='<main><h2>Session expired</h2></main>'}},1000);load();
</script></body></html>'''.replace("{TEMP_PAGE_CSS}", TEMP_PAGE_CSS)
