import asyncio
import logging
import os
import secrets
import signal
from html import escape
from pathlib import Path
from shutil import rmtree
from time import time

import psutil
from aiohttp import web

from ..core.config import Config
from ..core.logging_config import create_log_export, log_event
from ..core.models import AddOptions, Destination, Source, SourceType, Task, TaskPhase
from ..core.source_detector import detect_source
from ..downloaders.gdrive import drive_id_from_url
from .drive_sharing import DriveShareError, build_drive_share
from .google_drive_delivery import delete_drive_item, drive_item_info, drive_storage_quota, load_credentials, search_drive_items
from .jellyfin import JellyfinControlError, JellyfinManager
from .jellyfin_api import JellyfinApi
from .public_url import public_base_url
from .speedtest import SpeedtestError, run_speedtest
from .status import human_size
from .task_manager import TaskManager

LOGGER = logging.getLogger(__name__)
SESSION_COOKIE = "mirrorbot_session"


class WebDashboard:
    def __init__(
        self,
        config: Config,
        manager: TaskManager,
        background,
        telegram_client_getter,
        jellyfin: JellyfinManager,
        jellyfin_api: JellyfinApi,
        drive_search_pages,
        drive_share_pages,
        file_explorer_getter,
        schedule_local_metadata_refresh,
        schedule_series_promotion,
        completion_payload,
    ):
        self.config = config
        self.manager = manager
        self.background = background
        self.telegram_client_getter = telegram_client_getter
        self.jellyfin = jellyfin
        self.jellyfin_api = jellyfin_api
        self.drive_search_pages = drive_search_pages
        self.drive_share_pages = drive_share_pages
        self.file_explorer_getter = file_explorer_getter
        self.schedule_local_metadata_refresh = schedule_local_metadata_refresh
        self.schedule_series_promotion = schedule_series_promotion
        self.completion_payload = completion_payload
        self.sessions: set[str] = set()
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.speedtest_lock = asyncio.Lock()

    async def start(self) -> None:
        app = web.Application(client_max_size=8 * 1024**3, middlewares=[self.auth_middleware])
        app.router.add_get("/", self.index)
        app.router.add_get("/login", self.login_page)
        app.router.add_post("/login", self.login)
        app.router.add_post("/logout", self.logout)
        app.router.add_get("/api/state", self.api_state)
        app.router.add_post("/api/add", self.api_add)
        app.router.add_post("/api/upload", self.api_upload)
        app.router.add_post("/api/cancel/{task_id}", self.api_cancel)
        app.router.add_post("/api/cancelall", self.api_cancel_all)
        app.router.add_post("/api/jellyfin/{action}", self.api_jellyfin)
        app.router.add_post("/api/local", self.api_local)
        app.router.add_post("/api/drive/search", self.api_drive_search)
        app.router.add_post("/api/drive/share", self.api_drive_share)
        app.router.add_post("/api/drive/delete", self.api_drive_delete)
        app.router.add_get("/api/drive/stats", self.api_drive_stats)
        app.router.add_get("/api/logs", self.api_logs)
        app.router.add_post("/api/speedtest", self.api_speedtest)
        app.router.add_post("/api/restart", self.api_restart)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "0.0.0.0", self.config.web_port)
        await self.site.start()
        LOGGER.info("Web dashboard started port=%s", self.config.web_port)

    async def close(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
            self.site = None
            LOGGER.info("Web dashboard stopped")

    @web.middleware
    async def auth_middleware(self, request, handler):
        if request.path in {"/login"}:
            return await handler(request)
        if request.path.startswith("/api/") or request.path == "/":
            token = request.cookies.get(SESSION_COOKIE, "")
            if token not in self.sessions:
                if request.path.startswith("/api/"):
                    raise web.HTTPUnauthorized(text="Login required")
                raise web.HTTPFound("/login")
        return await handler(request)

    async def login_page(self, request: web.Request) -> web.Response:
        return web.Response(text=LOGIN_PAGE, content_type="text/html")

    async def login(self, request: web.Request) -> web.Response:
        data = await request.post()
        if not self.config.web_password:
            return web.Response(text="WEB_PASSWORD is not configured.", status=503)
        if secrets.compare_digest(data.get("username", ""), self.config.web_username) and secrets.compare_digest(data.get("password", ""), self.config.web_password):
            token = secrets.token_urlsafe(32)
            self.sessions.add(token)
            response = web.HTTPFound("/")
            response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Lax", max_age=7 * 24 * 3600)
            log_event(LOGGER, logging.INFO, "web.login", result="success")
            return response
        log_event(LOGGER, logging.WARNING, "web.login", result="failed")
        return web.Response(text="Invalid login", status=403)

    async def logout(self, request: web.Request) -> web.Response:
        token = request.cookies.get(SESSION_COOKIE, "")
        self.sessions.discard(token)
        response = web.HTTPFound("/login")
        response.del_cookie(SESSION_COOKIE)
        return response

    async def index(self, request: web.Request) -> web.Response:
        return web.Response(text=DASHBOARD_PAGE, content_type="text/html")

    def task_json(self, task: Task) -> dict:
        return {
            "id": task.short_id(),
            "full_id": task.id,
            "name": task.name or task.result_name or task.source.filename or task.source.type.value,
            "phase": task.phase.value,
            "source": task.source.type.value,
            "destination": task.destination.value,
            "current_file": task.current_file,
            "progress": round(task.progress * 100, 1) if task.size else None,
            "size": human_size(task.size) if task.size else "Unknown",
            "processed": human_size(task.downloaded),
            "speed": f"{human_size(task.speed)}/s" if task.speed else "-",
            "eta": task.eta,
            "error": task.error,
            "terminal": task.terminal,
            "selection_url": task.selection_url,
            "result": self.completion_payload(task) if task.terminal else None,
        }

    async def api_state(self, request: web.Request) -> web.Response:
        disk = psutil.disk_usage(str(self.config.local_download_root))
        tasks = list(self.manager.tasks.values())
        recent = sorted((task for task in tasks if task.terminal), key=lambda t: t.created_at, reverse=True)[:25]
        active = [task for task in tasks if not task.terminal]
        try:
            jellyfin_status = await asyncio.to_thread(self.jellyfin.status)
            jellyfin_state = {"state": jellyfin_status.state, "health": jellyfin_status.health, "running": jellyfin_status.running}
        except Exception:
            jellyfin_state = {"state": "unknown", "health": "unknown", "running": False}
        return web.json_response({
            "active": [self.task_json(task) for task in active],
            "recent": [self.task_json(task) for task in recent],
            "stats": {
                "cpu": psutil.cpu_percent(),
                "ram": psutil.virtual_memory().percent,
                "disk_free": human_size(disk.free),
                "disk_total": human_size(disk.total),
                "tasks": len(active),
                "jellyfin": jellyfin_state,
                "telegram_ui": self.config.enable_telegram_ui,
                "jellyfin_url": public_base_url(8003, self.config.public_base_url),
            },
        })

    def destination_from_form(self, destination: str, category: str = "") -> Destination:
        if destination == "local":
            return Destination.LOCAL_SERIES if category == "series" else Destination.LOCAL_MOVIES
        return Destination(destination)

    def options_from_data(self, data: dict) -> AddOptions:
        return AddOptions(
            name=str(data.get("name") or "").strip(),
            zip=bool(data.get("zip")),
            zip_password=str(data.get("zip_password") or ""),
            extract=bool(data.get("extract")),
            extract_password=str(data.get("extract_password") or ""),
            ytdlp_kind=str(data.get("ytdlp_kind") or ""),
            ytdlp_quality=str(data.get("ytdlp_quality") or ""),
        )

    async def api_add(self, request: web.Request) -> web.Response:
        data = await request.json()
        link = str(data.get("link") or "").strip()
        if not link:
            raise web.HTTPBadRequest(text="Link is required")
        source = detect_source(link)
        if source.type == SourceType.UNSUPPORTED:
            raise web.HTTPBadRequest(text="Unsupported source")
        destination = self.destination_from_form(str(data.get("destination") or ""), str(data.get("category") or ""))
        task = self.manager.create_task(self.config.owner_id, 0, 0, source, destination, self.options_from_data(data))
        self.spawn_transfer(task)
        log_event(LOGGER, logging.INFO, "web.add", task=task.short_id(), destination=destination.value, engine=source.type.value)
        return web.json_response({"ok": True, "task": task.short_id()})

    async def api_upload(self, request: web.Request) -> web.Response:
        reader = await request.multipart()
        fields: dict[str, str] = {}
        staging = self.config.download_dir / f"web-upload-{secrets.token_hex(8)}"
        staging.mkdir(parents=True, exist_ok=False)
        saved_files: list[Path] = []
        try:
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.filename:
                    filename = Path(part.filename).name or f"upload-{len(saved_files) + 1}"
                    target = staging / filename
                    if target.exists():
                        target = staging / f"{target.stem}-{secrets.token_hex(3)}{target.suffix}"
                    with target.open("wb") as output:
                        while True:
                            chunk = await part.read_chunk(1024 * 1024)
                            if not chunk:
                                break
                            await asyncio.to_thread(output.write, chunk)
                    saved_files.append(target)
                else:
                    fields[part.name] = (await part.text()).strip()
            if not saved_files:
                raise web.HTTPBadRequest(text="Select at least one file")
            destination = self.destination_from_form(fields.get("destination", ""), fields.get("category", ""))
            task = self.manager.create_task(self.config.owner_id, 0, 0, Source(SourceType.LOCAL_PATH, "", saved_files[0].name), destination, self.options_from_data(fields))
            upload_root = task.work_dir / "browser-upload"
            upload_root.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(upload_root)
            source_path = next(upload_root.iterdir()) if len(saved_files) == 1 else upload_root
            task.source.value = str(source_path)
            task.source.filename = source_path.name
            self.spawn_transfer(task)
            log_event(LOGGER, logging.INFO, "web.upload", task=task.short_id(), destination=destination.value, files=len(saved_files))
            return web.json_response({"ok": True, "task": task.short_id()})
        except Exception:
            rmtree(staging, ignore_errors=True)
            raise

    def spawn_transfer(self, task: Task) -> None:
        async def runner():
            await self.manager.run_task(task, telegram_client=self.telegram_client_getter())
            if task.destination in {Destination.LOCAL_MOVIES, Destination.LOCAL_SERIES}:
                self.schedule_local_metadata_refresh(task)
            if task.phase == TaskPhase.COMPLETE and task.destination == Destination.LOCAL_SERIES and not task.library_name.endswith(")"):
                self.schedule_series_promotion()
        self.manager.spawn(runner(), name="web-transfer-task")

    async def api_cancel(self, request: web.Request) -> web.Response:
        task_id = request.match_info["task_id"]
        if not self.manager.cancel(task_id):
            raise web.HTTPNotFound(text="Task not found or already finished")
        await self.manager.close_active_selector(task_id)
        return web.json_response({"ok": True})

    async def api_cancel_all(self, request: web.Request) -> web.Response:
        for task in self.manager.active_tasks():
            self.manager.cancel(task.id)
        await self.manager.close_active_selector()
        return web.json_response({"ok": True})

    async def api_jellyfin(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]
        try:
            if action == "start":
                status = await asyncio.to_thread(self.jellyfin.start)
                result = "started"
            elif action == "stop":
                status = await asyncio.to_thread(self.jellyfin.stop)
                result = "stopped"
            elif action == "restart":
                status = await asyncio.to_thread(self.jellyfin.restart)
                result = "restarted"
            elif action == "scan":
                result = await self.jellyfin_api.scan_and_refresh_metadata()
                status = await asyncio.to_thread(self.jellyfin.status)
            else:
                status = await asyncio.to_thread(self.jellyfin.status)
                result = "status"
        except (JellyfinControlError, Exception) as exc:
            LOGGER.exception("Web Jellyfin action failed action=%s", action)
            raise web.HTTPBadRequest(text=str(exc))
        log_event(LOGGER, logging.INFO, "web.jellyfin", action=action, result=result)
        return web.json_response({"ok": True, "result": str(result), "state": status.state, "health": status.health, "running": status.running})

    async def api_local(self, request: web.Request) -> web.Response:
        url = await self.file_explorer_getter().create(0)
        return web.json_response({"ok": True, "url": url})

    async def api_drive_search(self, request: web.Request) -> web.Response:
        data = await request.json()
        query = str(data.get("query") or "").strip()
        if not query:
            raise web.HTTPBadRequest(text="Search query is required")
        results = await asyncio.to_thread(search_drive_items, self.config, query, 100)
        if not results:
            return web.json_response({"ok": True, "count": 0, "url": ""})
        url = await self.drive_search_pages.create(query, results)
        return web.json_response({"ok": True, "count": len(results), "url": url})

    async def api_drive_share(self, request: web.Request) -> web.Response:
        data = await request.json()
        link = str(data.get("link") or "").strip()
        try:
            file_id = drive_id_from_url(link)
            manifest = await asyncio.to_thread(build_drive_share, self.config, file_id)
            url = await self.drive_share_pages.create(manifest)
            return web.json_response({"ok": True, "name": manifest.name, "files": len(manifest.files), "folders": manifest.folder_count, "url": url})
        except (DriveShareError, ValueError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def api_drive_delete(self, request: web.Request) -> web.Response:
        data = await request.json()
        value = str(data.get("id") or data.get("link") or "").strip()
        if not value:
            raise web.HTTPBadRequest(text="Drive link or ID is required")
        file_id = drive_id_from_url(value) if "http" in value else value
        item = await asyncio.to_thread(delete_drive_item, self.config, file_id)
        return web.json_response({"ok": True, "name": item.get("name", file_id)})

    async def api_drive_stats(self, request: web.Request) -> web.Response:
        credentials_exists = self.config.google_credentials_file.is_file()
        token_exists = self.config.google_token_file.is_file()
        if not credentials_exists or not token_exists:
            return web.json_response({"ready": False, "credentials": credentials_exists, "token": token_exists})
        await asyncio.to_thread(load_credentials, self.config)
        quota = await asyncio.to_thread(drive_storage_quota, self.config)
        return web.json_response({"ready": True, "quota": quota})

    async def api_logs(self, request: web.Request) -> web.Response:
        exported = await asyncio.to_thread(create_log_export, self.config.log_file)
        if exported is None:
            raise web.HTTPNotFound(text="No log file yet")
        try:
            text = Path(exported).read_text(encoding="utf-8", errors="replace")
        finally:
            Path(exported).unlink(missing_ok=True)
        return web.Response(text=text, content_type="text/plain")

    async def api_speedtest(self, request: web.Request) -> web.Response:
        if self.speedtest_lock.locked():
            raise web.HTTPConflict(text="A speed test is already running")
        async with self.speedtest_lock:
            try:
                result = await run_speedtest()
            except SpeedtestError as exc:
                raise web.HTTPBadRequest(text=str(exc))
        return web.json_response(result.__dict__)

    async def api_restart(self, request: web.Request) -> web.Response:
        log_event(LOGGER, logging.INFO, "web.restart", result="requested")
        async def restart_later():
            await asyncio.sleep(0.5)
            os.kill(1, signal.SIGTERM)
        self.background.create(restart_later(), name="web-restart")
        return web.json_response({"ok": True})


LOGIN_PAGE = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Mirror-Bot Login</title><style>body{font:15px system-ui;background:#f4f6f8;margin:0;display:grid;place-items:center;min-height:100vh;color:#182230}.box{background:#fff;border:1px solid #d8dde4;border-radius:8px;padding:24px;width:min(360px,92vw)}h1{margin:0 0 16px;font-size:24px}input,button{width:100%;padding:11px 12px;margin-top:10px;border-radius:6px;border:1px solid #c5ccd5;font:inherit}button{background:#1769e0;color:#fff;border-color:#1769e0;font-weight:700;cursor:pointer}</style></head><body><form class="box" method="post" action="/login"><h1>Mirror-Bot</h1><input name="username" placeholder="Username" autocomplete="username"><input name="password" type="password" placeholder="Password" autocomplete="current-password"><button>Login</button></form></body></html>'''

DASHBOARD_PAGE = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Mirror-Bot</title><style>*{box-sizing:border-box}body{font:14px system-ui;margin:0;background:#f4f6f8;color:#182230}header{background:#fff;border-bottom:1px solid #dfe4ea}.top{max-width:1240px;margin:auto;padding:18px}h1{font-size:24px;margin:0}.sub{color:#667085;margin-top:4px}main{max-width:1240px;margin:18px auto;padding:0 18px;display:grid;gap:16px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.card{background:#fff;border:1px solid #d8dde4;border-radius:8px;padding:14px}.card h2{font-size:16px;margin:0 0 10px}.stat{font-size:22px;font-weight:750}input,select,button{padding:9px 10px;border:1px solid #c5ccd5;border-radius:6px;font:inherit}button{background:#1769e0;color:#fff;border-color:#1769e0;font-weight:700;cursor:pointer}.secondary{background:#fff;color:#182230;border-color:#c5ccd5}.danger{background:#b42318;border-color:#b42318}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.row>*{min-width:120px}textarea{width:100%;min-height:70px;padding:10px;border:1px solid #c5ccd5;border-radius:6px;font:inherit}.tasks{display:grid;gap:10px}.task{border:1px solid #e1e6ee;border-radius:8px;padding:12px;background:#fff}.muted{color:#667085}.bar{height:9px;background:#eef2f6;border-radius:999px;overflow:hidden;margin:8px 0}.bar span{display:block;height:100%;background:#1769e0}.links a{display:inline-block;margin:4px 6px 0 0}.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:850px){.grid,.two{grid-template-columns:1fr}.row>*{flex:1}}</style></head><body><header><div class="top"><h1>Mirror-Bot</h1><div class="sub">Web dashboard on port 8000</div></div></header><main><section class="grid" id="stats"></section><section class="card"><h2>Add link</h2><div class="row"><input id="link" style="flex:3" placeholder="URL, magnet, Google Drive, BuzzHeavier, yt-dlp"><select id="destination"><option value="local">Local</option><option value="telegram">Telegram</option><option value="google_drive">Google Drive</option><option value="buzzheavier">BuzzHeavier</option></select><select id="category"><option value="movies">Movies</option><option value="series">Series</option></select><input id="name" placeholder="Custom name"><button onclick="addLink()">Add</button></div><div class="row" style="margin-top:8px"><label><input type="checkbox" id="zip"> Zip</label><input id="zip_password" placeholder="Zip password"><label><input type="checkbox" id="extract"> Extract</label><input id="extract_password" placeholder="Extract password"><select id="ytdlp_kind"><option value="">yt-dlp auto</option><option value="video">Video</option><option value="audio">Audio</option></select><input id="ytdlp_quality" placeholder="Quality e.g. 1080 or 320"></div></section><section class="card"><h2>Upload from browser</h2><form id="uploadForm"><div class="row"><input type="file" name="file" multiple><select name="destination"><option value="local">Local</option><option value="telegram">Telegram</option><option value="google_drive">Google Drive</option><option value="buzzheavier">BuzzHeavier</option></select><select name="category"><option value="movies">Movies</option><option value="series">Series</option></select><input name="name" placeholder="Custom name"><button>Upload</button></div></form></section><section class="two"><div class="card"><h2>Active tasks</h2><div class="row"><button class="danger" onclick="cancelAll()">Cancel all</button><button class="secondary" onclick="openLocal()">Open local files</button><a id="jellyfinLink" class="secondary" href="#" target="_blank"><button class="secondary">Open Jellyfin</button></a></div><div id="active" class="tasks" style="margin-top:12px"></div></div><div class="card"><h2>Recent tasks</h2><div id="recent" class="tasks"></div></div></section><section class="two"><div class="card"><h2>Google Drive</h2><div class="row"><input id="driveQuery" placeholder="Search name"><button onclick="driveSearch()">Search</button></div><div class="row" style="margin-top:8px"><input id="shareLink" placeholder="Public Drive link"><button onclick="driveShare()">Share</button></div><div class="row" style="margin-top:8px"><input id="deleteDrive" placeholder="Drive link or ID"><button class="danger" onclick="driveDelete()">Delete</button><button class="secondary" onclick="driveStats()">Stats</button></div><div id="driveOut" class="muted"></div></div><div class="card"><h2>Admin</h2><div class="row"><button onclick="jellyfin('scan')">Scan Jellyfin</button><button class="secondary" onclick="jellyfin('restart')">Restart Jellyfin</button><button class="secondary" onclick="jellyfin('start')">Start Jellyfin</button><button class="secondary" onclick="jellyfin('stop')">Stop Jellyfin</button><button onclick="speedtest()">Speedtest</button><button class="secondary" onclick="logs()">Logs</button><button class="danger" onclick="restart()">Restart Bot</button></div><pre id="adminOut" class="muted"></pre></div></section></main><script>
async function api(path, opts={}){const r=await fetch(path,opts);if(!r.ok)throw Error(await r.text());return r.headers.get('content-type')?.includes('application/json')?r.json():r.text()}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function taskHtml(t){const pct=t.progress??0;const links=t.result?.links||[];return `<div class="task"><b>${esc(t.id)}</b> ${esc(t.name)}<div class="muted">${esc(t.phase)} -> ${esc(t.destination)} | ${esc(t.processed)} / ${esc(t.size)} | ${esc(t.speed)}</div>${t.progress!==null?`<div class="bar"><span style="width:${pct}%"></span></div>`:''}${t.selection_url?`<a href="${esc(t.selection_url)}" target="_blank">Open selector</a>`:''}${!t.terminal?` <button class="danger" onclick="cancelTask('${t.id}')">Cancel</button>`:''}${t.error?`<pre>${esc(t.error)}</pre>`:''}<div class="links">${links.map((l,i)=>`<a href="${esc(l.url)}" target="_blank">${esc(l.label||('Open '+(i+1)))}</a>`).join('')}</div></div>`}
async function refresh(){try{const s=await api('/api/state');document.querySelector('#stats').innerHTML=`<div class="card"><div class="muted">CPU</div><div class="stat">${s.stats.cpu}%</div></div><div class="card"><div class="muted">RAM</div><div class="stat">${s.stats.ram}%</div></div><div class="card"><div class="muted">Free</div><div class="stat">${s.stats.disk_free}</div></div><div class="card"><div class="muted">Tasks</div><div class="stat">${s.stats.tasks}</div></div>`;syncTelegramOptions(s.stats.telegram_ui);document.querySelector('#jellyfinLink').href=s.stats.jellyfin_url;document.querySelector('#active').innerHTML=s.active.map(taskHtml).join('')||'<div class="muted">No active tasks</div>';document.querySelector('#recent').innerHTML=s.recent.map(taskHtml).join('')||'<div class="muted">No recent tasks</div>'}catch(e){console.error(e)}}
function syncTelegramOptions(enabled){document.querySelectorAll('option[value=\"telegram\"]').forEach(o=>{if(!enabled)o.remove()})}
async function addLink(){const data={link:link.value,destination:destination.value,category:category.value,name:name.value,zip:zip.checked,zip_password:zip_password.value,extract:extract.checked,extract_password:extract_password.value,ytdlp_kind:ytdlp_kind.value,ytdlp_quality:ytdlp_quality.value};await api('/api/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});link.value='';refresh()}
document.querySelector('#uploadForm').onsubmit=async e=>{e.preventDefault();await api('/api/upload',{method:'POST',body:new FormData(e.target)});e.target.reset();refresh()}
async function cancelTask(id){await api('/api/cancel/'+id,{method:'POST'});refresh()} async function cancelAll(){await api('/api/cancelall',{method:'POST'});refresh()}
async function openLocal(){const r=await api('/api/local',{method:'POST'});window.open(r.url,'_blank')}
async function jellyfin(a){adminOut.textContent=JSON.stringify(await api('/api/jellyfin/'+a,{method:'POST'}),null,2)}
async function driveSearch(){const r=await api('/api/drive/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:driveQuery.value})});driveOut.innerHTML=r.url?`Found ${r.count}: <a href="${r.url}" target="_blank">Open results</a>`:'No results'}
async function driveShare(){const r=await api('/api/drive/share',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({link:shareLink.value})});driveOut.innerHTML=`Share: <a href="${r.url}" target="_blank">${esc(r.name)}</a>`}
async function driveDelete(){if(!confirm('Delete this Drive item permanently?'))return;driveOut.textContent=JSON.stringify(await api('/api/drive/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:deleteDrive.value})}))}
async function driveStats(){driveOut.textContent=JSON.stringify(await api('/api/drive/stats'),null,2)}
async function speedtest(){adminOut.textContent='Running speedtest...';adminOut.textContent=JSON.stringify(await api('/api/speedtest',{method:'POST'}),null,2)}
async function logs(){window.open('/api/logs','_blank')} async function restart(){if(confirm('Restart Mirror-Bot?'))await api('/api/restart',{method:'POST'})}
setInterval(refresh,3000);refresh();</script></body></html>'''
