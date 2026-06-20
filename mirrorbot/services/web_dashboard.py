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
        return await handler(request)

    async def login_page(self, request: web.Request) -> web.Response:
        raise web.HTTPFound("/")

    async def login(self, request: web.Request) -> web.Response:
        raise web.HTTPFound("/")

    async def logout(self, request: web.Request) -> web.Response:
        token = request.cookies.get(SESSION_COOKIE, "")
        self.sessions.discard(token)
        response = web.HTTPFound("/")
        response.del_cookie(SESSION_COOKIE)
        return response

    async def index(self, request: web.Request) -> web.Response:
        return web.Response(text=DASHBOARD_PAGE, content_type="text/html")

    @staticmethod
    def display_name(task: Task) -> str:
        if task.terminal:
            if task.destination in {Destination.LOCAL_MOVIES, Destination.LOCAL_SERIES}:
                return task.library_name or task.result_name or task.name or task.source.filename or task.source.type.value
            return task.result_name or task.name or task.source.filename or task.source.type.value
        return task.name or task.source.filename or task.result_name or task.source.type.value

    def task_json(self, task: Task) -> dict:
        return {
            "id": task.short_id(),
            "full_id": task.id,
            "name": self.display_name(task),
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


DASHBOARD_PAGE = r'''<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Mirror-Bot</title>
<style>
:root{color-scheme:light;--bg:#f5f7fb;--surface:#fff;--surface2:#f8fafc;--text:#182230;--muted:#667085;--line:#dde4ee;--line2:#c9d2df;--blue:#1769e0;--blue2:#eaf2ff;--green:#079455;--amber:#b54708;--red:#b42318;--purple:#7047eb;--shadow:0 12px 36px rgba(16,24,40,.08)}
html[data-theme=dark]{color-scheme:dark;--bg:#0f141b;--surface:#161d26;--surface2:#111821;--text:#e8edf5;--muted:#9aa7b8;--line:#252f3d;--line2:#334052;--blue:#63a0ff;--blue2:#14233a;--green:#35c887;--amber:#f2a23a;--red:#ff6b5f;--purple:#a794ff;--shadow:none}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,-apple-system,Segoe UI,sans-serif}button,input,select{font:inherit}button{border:0;border-radius:7px;background:var(--blue);color:#fff;font-weight:750;padding:9px 13px;cursor:pointer;min-height:38px}button:hover{filter:brightness(.96)}button.secondary{background:var(--surface);color:var(--text);border:1px solid var(--line2)}button.ghost{background:transparent;color:var(--text);border:1px solid var(--line)}button.danger{background:var(--red)}a{color:var(--blue);text-decoration:none}.shell{max-width:1260px;margin:0 auto;padding:0 20px}.topbar{position:sticky;top:0;z-index:10;background:color-mix(in srgb,var(--surface) 94%,transparent);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}.toprow{display:flex;align-items:center;gap:16px;min-height:68px}.brand{display:flex;align-items:center;gap:11px;font-weight:850;font-size:20px}.mark{width:34px;height:34px;border-radius:8px;background:linear-gradient(135deg,var(--blue),var(--purple));color:#fff;display:grid;place-items:center;font-weight:900}.topactions{margin-left:auto;display:flex;align-items:center;gap:8px}.pill{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);border-radius:999px;padding:6px 10px;background:var(--surface2);color:var(--muted);white-space:nowrap}.dot{width:8px;height:8px;border-radius:50%;background:var(--green)}.dot.warn{background:var(--amber)}.dot.off{background:var(--red)}.tabs{display:flex;gap:5px;overflow:auto;padding:0 0 12px}.tab{background:transparent;color:var(--muted);border:0;border-radius:7px;padding:8px 11px;white-space:nowrap}.tab.active{background:var(--text);color:var(--bg)}.shell.content{padding:24px 20px 36px}.view{display:none}.view.active{display:block}.pagehead{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:15px}.pagehead h1{font-size:25px;line-height:1.15;margin:0}.pagehead p{margin:5px 0 0;color:var(--muted)}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}.metric{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:15px;box-shadow:var(--shadow)}.metric .label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}.metric .value{font-size:24px;font-weight:850;margin-top:4px}.grid2{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:14px}.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.panel{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:16px;box-shadow:var(--shadow)}.panel h2{font-size:16px;margin:0 0 14px}.panel h3{font-size:12px;color:var(--muted);text-transform:uppercase;margin:18px 0 8px;letter-spacing:.04em}.stack{display:grid;gap:10px}.quickgrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.quickcard{border:1px solid var(--line);border-radius:8px;background:var(--surface2);padding:14px;text-align:left;color:var(--text);min-height:92px}.quickcard strong{display:block;font-size:15px;margin-bottom:5px}.quickcard span{color:var(--muted);font-size:13px}.formgrid{display:grid;grid-template-columns:minmax(220px,2fr) 160px 130px minmax(160px,1fr) auto;gap:9px}.uploadgrid{display:grid;grid-template-columns:minmax(200px,1fr) 160px 130px minmax(160px,1fr) auto;gap:9px}.twocol{display:grid;grid-template-columns:1fr 1fr;gap:9px}.field{display:grid;gap:5px}.field span{font-size:12px;color:var(--muted)}input,select{width:100%;border:1px solid var(--line2);border-radius:7px;background:var(--surface);padding:9px 10px;color:var(--text);min-height:38px}.checks{display:flex;gap:14px;align-items:center;flex-wrap:wrap}.checks label{display:flex;align-items:center;gap:6px;color:var(--text)}details.advanced{margin-top:12px;border:1px solid var(--line);border-radius:8px;background:var(--surface2);padding:11px}details.advanced summary{cursor:pointer;font-weight:750}.tasks{display:grid;gap:10px}.task{border:1px solid var(--line);border-radius:8px;background:var(--surface);padding:13px}.task.compact{box-shadow:none}.taskhead{display:flex;gap:10px;align-items:flex-start}.taskid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--blue);font-weight:850}.taskname{font-weight:800;overflow-wrap:anywhere}.badge{margin-left:auto;color:var(--blue);font-size:12px;background:var(--blue2);border:1px solid color-mix(in srgb,var(--blue) 30%,transparent);border-radius:999px;padding:4px 8px;white-space:nowrap}.muted{color:var(--muted)}.bar{height:9px;background:var(--surface2);border-radius:99px;overflow:hidden;margin:10px 0;border:1px solid var(--line)}.bar span{display:block;height:100%;background:linear-gradient(90deg,var(--blue),var(--green))}.taskmeta{display:flex;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:13px}.taskactions,.rowactions{display:flex;gap:8px;flex-wrap:wrap;margin-top:11px}.empty{color:var(--muted);border:1px dashed var(--line2);border-radius:8px;padding:22px;text-align:center;background:var(--surface2)}.output{white-space:pre-wrap;background:var(--surface2);border:1px solid var(--line);border-radius:8px;padding:11px;min-height:46px;color:var(--muted);overflow:auto}.resultbox a,.links a{display:inline-flex;margin:6px 8px 0 0}.toast{position:fixed;right:18px;bottom:18px;background:var(--text);color:var(--bg);padding:12px 15px;border-radius:8px;display:none;z-index:30;box-shadow:var(--shadow)}.footerhint{margin-top:12px;color:var(--muted);font-size:13px}@media(max-width:960px){.metrics,.grid3{grid-template-columns:repeat(2,1fr)}.grid2{grid-template-columns:1fr}.formgrid,.uploadgrid,.twocol{grid-template-columns:1fr}.toprow{align-items:flex-start;padding:14px 0}.topactions{flex-wrap:wrap;justify-content:flex-end}.shell{padding:0 14px}.shell.content{padding:24px 14px 36px}}@media(max-width:560px){.metrics,.grid3,.quickgrid{grid-template-columns:1fr}.taskhead{display:grid}.badge{margin-left:0;justify-self:start}.rowactions button,.taskactions button{width:100%}.pagehead{display:grid}.topactions .pill{display:none}}
 </style>
<style>
.smartadd{display:grid;gap:16px;max-width:1040px;margin:0 auto}.smartadd h3{margin:2px 0 9px}.modebar{display:inline-grid;grid-template-columns:repeat(2,1fr);gap:4px;background:var(--surface2);border:1px solid var(--line);border-radius:8px;padding:4px;width:max-content}.modebar button{background:transparent;color:var(--muted);min-width:112px}.modebar button.active{background:var(--text);color:var(--bg)}.sourcebox{display:grid;gap:9px}.sourcebox textarea{width:100%;min-height:98px;resize:vertical;border:1px solid var(--line2);border-radius:8px;background:var(--surface);color:var(--text);padding:15px;font:inherit;font-size:15px;line-height:1.45}.sourcebox textarea:focus,input:focus{outline:2px solid color-mix(in srgb,var(--blue) 35%,transparent);border-color:var(--blue)}.sourcehint{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;color:var(--muted);min-height:28px}.detect{display:inline-flex;align-items:center;gap:7px;background:var(--blue2);color:var(--blue);border:1px solid color-mix(in srgb,var(--blue) 28%,transparent);border-radius:999px;padding:5px 9px;font-weight:750}.choicegrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}.choicecard{border:1px solid var(--line);border-radius:8px;background:var(--surface2);color:var(--text);padding:14px;text-align:left;min-height:86px;position:relative}.choicecard:hover{border-color:var(--line2);filter:brightness(1.03)}.choicecard strong{display:block;font-size:15px}.choicecard span{display:block;color:var(--muted);font-size:12px;line-height:1.35;margin-top:5px}.choicecard.active{border-color:var(--blue);background:var(--blue2);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--blue) 35%,transparent)}.choicecard.active:after{content:"Selected";position:absolute;right:10px;top:10px;color:var(--blue);font-size:11px;font-weight:850}.compactrow{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.segmented,.qualitygrid{display:inline-flex;gap:4px;background:var(--surface2);border:1px solid var(--line);border-radius:8px;padding:4px;flex-wrap:wrap}.segmented button,.qualitygrid button{background:transparent;color:var(--muted);min-height:34px}.segmented button.active,.qualitygrid button.active{background:var(--blue);color:#fff}.qualitypanel{display:grid;gap:10px;border:1px solid var(--line);border-radius:8px;background:var(--surface2);padding:12px}.qualityhint{color:var(--muted);font-size:12px}.addmode{display:none}.addmode.active{display:grid;gap:14px}.uploaddrop{border:1px dashed var(--line2);border-radius:8px;background:var(--surface2);padding:0;overflow:hidden}.uploadpick{display:grid;place-items:center;text-align:center;gap:8px;min-height:150px;padding:24px;cursor:pointer}.uploadpick:hover{background:color-mix(in srgb,var(--blue2) 55%,transparent)}.uploadpick input{position:absolute;inline-size:1px;block-size:1px;opacity:0;pointer-events:none}.uploadicon{width:44px;height:44px;border-radius:999px;background:var(--blue2);color:var(--blue);display:grid;place-items:center;font-size:22px;font-weight:900}.uploadtitle{font-weight:850;font-size:16px}.uploadsub{color:var(--muted);font-size:13px}.uploadmeta{border-top:1px solid var(--line);padding:10px 14px;color:var(--muted);font-size:13px;background:var(--surface)}.uploadmeta strong{color:var(--text)}.smartfooter{display:flex;justify-content:flex-end;gap:8px;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:14px}.smartfooter button{min-width:140px}@media(max-width:560px){.choicegrid{grid-template-columns:1fr}.modebar{width:100%}.smartfooter button{width:100%}}
</style></head>
<body><header class="topbar"><div class="shell"><div class="toprow"><div class="brand"><div class="mark">M</div><div>Mirror-Bot</div></div><div class="topactions"><span class="pill"><span id="jfDot" class="dot warn"></span><span id="jfState">Jellyfin</span></span><span class="pill" id="tgState">Web UI</span><button class="ghost" id="themeToggle" type="button">Theme</button></div></div><nav class="tabs" aria-label="Main navigation"><button class="tab active" data-view="home">Home</button><button class="tab" data-view="add">Add</button><button class="tab" data-view="status">Status</button><button class="tab" data-view="files">Files</button><button class="tab" data-view="drive">Drive</button><button class="tab" data-view="jellyfin">Jellyfin</button><button class="tab" data-view="admin">Admin</button></nav></div></header>
<main class="shell content"><section class="metrics" id="stats"></section>
<section id="home" class="view active"><div class="pagehead"><div><h1>Overview</h1><p>Quick control for transfers, storage, Jellyfin, and cloud tools.</p></div><button onclick="showView('add')">New task</button></div><div class="grid2"><div class="panel"><h2>Quick Actions</h2><div class="quickgrid"><button class="quickcard" onclick="showView('add')"><strong>Add link</strong><span>Direct, torrent, Drive, yt-dlp, BuzzHeavier</span></button><button class="quickcard" onclick="showView('status')"><strong>Task status</strong><span>Live queue, progress, selectors, cancels</span></button><button class="quickcard" onclick="openLocal()"><strong>File explorer</strong><span>Browse, manage, upload local media</span></button><button class="quickcard" onclick="showView('drive')"><strong>Google Drive</strong><span>Search, share, delete, quota</span></button><button class="quickcard" onclick="showView('jellyfin')"><strong>Jellyfin</strong><span>Open server, scan, restart service</span></button><button class="quickcard" onclick="showView('admin')"><strong>Admin</strong><span>Logs, speedtest, restart bot</span></button></div></div><aside class="panel"><h2>Recent Activity</h2><div id="homeRecent" class="tasks"></div></aside></div></section>
<section id="add" class="view"><div class="pagehead"><div><h1>Add</h1><p>Paste a link or choose files. The page will show only the controls that matter.</p></div></div><div class="panel smartadd"><div class="compactrow"><h2 style="margin:0">Add anything</h2><div class="modebar"><button id="linkModeBtn" class="active" type="button" onclick="setAddMode('link')">Link</button><button id="uploadModeBtn" type="button" onclick="setAddMode('upload')">Upload</button></div></div><div id="linkMode" class="addmode active"><div class="sourcebox"><textarea id="link" placeholder="Paste a URL, magnet, Google Drive link, BuzzHeavier link, or media page"></textarea><div class="sourcehint"><span class="detect">Detected as <strong id="sourceBadge">Unknown</strong></span><span id="sourceHint">Paste a source to begin.</span></div></div></div><form id="uploadForm" class="addmode"><div class="uploaddrop"><label class="uploadpick" for="uploadFiles"><input id="uploadFiles" type="file" name="file" multiple><span class="uploadicon">+</span><span class="uploadtitle">Select files to upload</span><span class="uploadsub">Choose one or more files from this browser. Destination and processing options stay below.</span></label><div class="uploadmeta" id="uploadMeta">No files selected</div></div></form><div><h3>Destination</h3><div class="choicegrid" id="destinationCards"><button class="choicecard active" type="button" data-destination="local"><strong>Local</strong><span>Save into Movies or Series and scan Jellyfin.</span></button><button class="choicecard" type="button" data-destination="telegram"><strong>Telegram</strong><span>Upload back to your chat when enabled.</span></button><button class="choicecard" type="button" data-destination="google_drive"><strong>Google Drive</strong><span>Upload to your configured Drive folder.</span></button><button class="choicecard" type="button" data-destination="buzzheavier"><strong>BuzzHeavier</strong><span>Upload and return a public file link.</span></button></div></div><div id="categoryWrap"><h3>Local library</h3><div class="segmented" id="categoryCards"><button class="active" type="button" data-category="movies">Movies</button><button type="button" data-category="series">Series</button></div></div><div id="ytdlpWrap" style="display:none"><h3>yt-dlp format</h3><div class="qualitypanel"><div class="segmented" id="ytdlpKindCards"><button class="active" type="button" data-ytdlp-kind="video">Video</button><button type="button" data-ytdlp-kind="audio">Audio</button></div><div class="qualitygrid" id="videoQualityCards"><button type="button" data-ytdlp-quality="360">360p</button><button type="button" data-ytdlp-quality="480">480p</button><button type="button" data-ytdlp-quality="720">720p</button><button class="active" type="button" data-ytdlp-quality="1080">1080p</button></div><div class="qualitygrid" id="audioQualityCards" style="display:none"><button type="button" data-ytdlp-quality="64">64 kbps</button><button type="button" data-ytdlp-quality="128">128 kbps</button><button type="button" data-ytdlp-quality="192">192 kbps</button><button type="button" data-ytdlp-quality="256">256 kbps</button><button class="active" type="button" data-ytdlp-quality="320">320 kbps</button></div><div class="qualityhint" id="qualityHint">Video up to 1080p with audio.</div></div></div><details class="advanced"><summary>Processing options</summary><div class="twocol" style="margin-top:10px"><label class="field"><span>Custom name</span><input id="name" placeholder="Optional"></label><label class="field"><span>Zip password</span><input id="zip_password" placeholder="Optional"></label><label class="field"><span>Extract password</span><input id="extract_password" placeholder="Optional"></label></div><div class="checks" style="margin-top:10px"><label><input type="checkbox" id="zip"> Zip after download</label><label><input type="checkbox" id="extract"> Extract after download</label></div></details><div class="smartfooter"><button id="addPrimary" type="button" onclick="submitSmartAdd()">Start task</button></div></div></section>
<section id="status" class="view"><div class="pagehead"><div><h1>Status</h1><p>Live transfers, selectors, cancellation, and task history.</p></div><button class="danger" onclick="cancelAll()">Cancel all</button></div><div class="grid2"><div class="panel"><h2>Active Tasks</h2><div id="active" class="tasks"></div></div><aside class="panel"><h2>Completed Tasks</h2><div id="recent" class="tasks"></div></aside></div></section>
<section id="files" class="view"><div class="pagehead"><div><h1>Files</h1><p>Manage local media and open Jellyfin.</p></div></div><div class="grid3"><div class="panel"><h2>Local Explorer</h2><p class="muted">Open a temporary file manager for the mounted downloads folder.</p><div class="rowactions"><button onclick="openLocal()">Open file explorer</button></div></div><div class="panel"><h2>Jellyfin</h2><p class="muted">Open the media server or scan newly delivered content.</p><div class="rowactions"><a id="jellyfinLink" href="#" target="_blank"><button class="secondary">Open Jellyfin</button></a><button onclick="jellyfin('scan','fileOut')">Scan library</button></div></div><div class="panel"><h2>Browser Upload</h2><p class="muted">Use the Add page to upload files into local, Drive, Telegram, or BuzzHeavier.</p><div class="rowactions"><button class="secondary" onclick="showView('add')">Upload files</button></div></div></div><pre id="fileOut" class="output" style="margin-top:14px"></pre></section>
<section id="drive" class="view"><div class="pagehead"><div><h1>Google Drive</h1><p>Search, share, delete, and inspect Drive quota.</p></div><button class="secondary" onclick="driveStats()">Quota</button></div><div class="grid2"><div class="panel"><h2>Drive Tools</h2><h3>Search</h3><div class="formgrid" style="grid-template-columns:1fr auto"><input id="driveQuery" placeholder="File or folder name"><button onclick="driveSearch()">Search</button></div><h3>Temporary public share</h3><div class="formgrid" style="grid-template-columns:1fr auto"><input id="shareLink" placeholder="Public Drive link"><button onclick="driveShare()">Share</button></div><h3>Delete</h3><div class="formgrid" style="grid-template-columns:1fr auto"><input id="deleteDrive" placeholder="Drive link or ID"><button class="danger" onclick="driveDelete()">Delete</button></div></div><aside class="panel"><h2>Result</h2><div id="driveOut" class="output resultbox"></div></aside></div></section>
<section id="jellyfin" class="view"><div class="pagehead"><div><h1>Jellyfin</h1><p>Control the companion media server and library scans.</p></div><a id="jellyfinLink2" href="#" target="_blank"><button>Open Jellyfin</button></a></div><div class="grid3"><div class="panel"><h2>Status</h2><div id="jellyfinSummary" class="stack"></div></div><div class="panel"><h2>Library</h2><p class="muted">Run a full scan and metadata refresh.</p><div class="rowactions"><button onclick="jellyfin('scan','jellyfinOut')">Scan library</button></div></div><div class="panel"><h2>Service</h2><div class="rowactions"><button class="secondary" onclick="jellyfin('restart','jellyfinOut')">Restart</button><button class="secondary" onclick="jellyfin('start','jellyfinOut')">Start</button><button class="danger" onclick="jellyfin('stop','jellyfinOut')">Stop</button></div></div></div><pre id="jellyfinOut" class="output" style="margin-top:14px"></pre></section>
<section id="admin" class="view"><div class="pagehead"><div><h1>Admin</h1><p>Diagnostics, logs, speed testing, and bot restart.</p></div></div><div class="panel"><h2>Controls</h2><div class="rowactions"><button onclick="speedtest()">Speedtest</button><button class="secondary" onclick="logs()">Logs</button><button class="danger" onclick="restart()">Restart Bot</button></div><pre id="adminOut" class="output" style="margin-top:12px"></pre></div></section></main><div id="toast" class="toast"></div>
<script>
const qs=s=>document.querySelector(s),qsa=s=>[...document.querySelectorAll(s)];let lastState=null;
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function api(path,opts={}){const r=await fetch(path,opts);if(!r.ok)throw Error(await r.text());return r.headers.get('content-type')?.includes('application/json')?r.json():r.text()}
function toast(msg){const t=qs('#toast');t.textContent=msg;t.style.display='block';clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>t.style.display='none',2800)}
function setTheme(mode){if(mode==='auto')localStorage.removeItem('mirror.theme');else localStorage.setItem('mirror.theme',mode);applyTheme()}
function applyTheme(){const saved=localStorage.getItem('mirror.theme');const dark=saved?saved==='dark':matchMedia('(prefers-color-scheme: dark)').matches;document.documentElement.dataset.theme=dark?'dark':'light';qs('#themeToggle').textContent=dark?'Light':'Dark'}
function showView(id){qsa('.view').forEach(v=>v.classList.toggle('active',v.id===id));qsa('.tab').forEach(t=>t.classList.toggle('active',t.dataset.view===id));qs('#stats').style.display=(id==='home'||id==='status')?'grid':'none';location.hash=id;refresh()}
qsa('.tab').forEach(t=>t.onclick=()=>showView(t.dataset.view));qs('#themeToggle').onclick=()=>setTheme(document.documentElement.dataset.theme==='dark'?'light':'dark');matchMedia('(prefers-color-scheme: dark)').addEventListener('change',applyTheme);applyTheme();
addEventListener('hashchange',()=>{const id=(location.hash||'#home').slice(1);if(qs('#'+id))showView(id)});
function taskHtml(t,compact=false){const pct=t.progress??0,links=t.result?.links||[];return `<div class="task ${compact?'compact':''}"><div class="taskhead"><div><div><span class="taskid">${esc(t.id)}</span> <span class="taskname">${esc(t.name)}</span></div><div class="muted">${esc(t.destination)} / ${esc(t.source)}</div></div><span class="badge">${esc(t.phase)}</span></div>${t.progress!==null?`<div class="bar"><span style="width:${pct}%"></span></div>`:''}<div class="taskmeta"><span>${esc(t.processed)} / ${esc(t.size)}</span><span>${esc(t.speed)}</span>${t.eta?`<span>ETA ${esc(t.eta)}</span>`:''}${t.current_file?`<span>${esc(t.current_file)}</span>`:''}</div>${t.error?`<pre class="output">${esc(t.error)}</pre>`:''}<div class="taskactions">${t.selection_url?`<a href="${esc(t.selection_url)}" target="_blank"><button>Selector</button></a>`:''}${!t.terminal?`<button class="danger" onclick="cancelTask('${t.id}')">Cancel</button>`:''}<span class="links">${links.map((l,i)=>`<a href="${esc(l.url)}" target="_blank">${esc(l.label||('Open '+(i+1)))}</a>`).join('')}</span></div></div>`}
function metric(label,value){return `<div class="metric"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`}
function appUrl(url){try{const u=new URL(url,location.href);u.protocol=location.protocol;u.hostname=location.hostname;return u.toString()}catch{return url}}
let addMode='link',selectedDestination='local',selectedCategory='movies',selectedYtdlpKind='video',selectedYtdlpQuality='1080';
function detectSource(value){const v=value.trim().toLowerCase();if(!v)return ['Unknown','Paste a source to begin.'];if(v.startsWith('magnet:?'))return ['Torrent','File selection will open after metadata is ready.'];if(v.includes('drive.google.com'))return ['Google Drive','Drive links can be downloaded or uploaded elsewhere.'];if(v.includes('buzzheavier.com'))return ['BuzzHeavier','BuzzHeavier links are supported directly.'];if(v.includes('youtube.com')||v.includes('youtu.be')||v.includes('instagram.com')||v.includes('tiktok.com')||v.includes('twitter.com')||v.includes('x.com'))return ['yt-dlp','Choose video or audio options below.'];if(v.startsWith('http://')||v.startsWith('https://'))return ['Direct link','Ready for direct download.'];return ['Unknown','The backend will validate this when submitted.']}
function setAddMode(mode){addMode=mode;qsa('.addmode').forEach(el=>el.classList.toggle('active',el.id===(mode==='link'?'linkMode':'uploadForm')));qs('#linkModeBtn').classList.toggle('active',mode==='link');qs('#uploadModeBtn').classList.toggle('active',mode==='upload');updateSmartAdd()}
function setSelected(group,attr,value){qsa(`#${group} [data-${attr}]`).forEach(b=>b.classList.toggle('active',b.dataset[attr.replaceAll('-','')]===value))}
function updateSmartAdd(){const [kind,hint]=detectSource(qs('#link').value);qs('#sourceBadge').textContent=kind;qs('#sourceHint').textContent=hint;qs('#categoryWrap').style.display=selectedDestination==='local'?'block':'none';qs('#ytdlpWrap').style.display=(addMode==='link'&&kind==='yt-dlp')?'block':'none';qs('#addPrimary').textContent=addMode==='upload'?'Start upload':'Start task'}
function updateYtdlpQuality(){const audio=selectedYtdlpKind==='audio';qs('#videoQualityCards').style.display=audio?'none':'inline-flex';qs('#audioQualityCards').style.display=audio?'inline-flex':'none';qs('#qualityHint').textContent=audio?`Audio MP3 at ${selectedYtdlpQuality} kbps.`:`Video up to ${selectedYtdlpQuality}p with audio.`}
function updateUploadMeta(){const files=[...(qs('#uploadFiles')?.files||[])];qs('#uploadMeta').innerHTML=files.length?`<strong>${files.length}</strong> file${files.length===1?'':'s'} selected: ${esc(files.slice(0,3).map(f=>f.name).join(', '))}${files.length>3?' ...':''}`:'No files selected'}
function smartPayload(){const extract=qs('#extract').checked;const zip=qs('#zip').checked&&!extract;return {destination:selectedDestination,category:selectedCategory,name:qs('#name').value,zip,zip_password:qs('#zip_password').value,extract,extract_password:qs('#extract_password').value,ytdlp_kind:selectedYtdlpKind,ytdlp_quality:selectedYtdlpQuality}}
function syncTelegramOptions(enabled){const card=qs('[data-destination="telegram"]');if(card){card.disabled=!enabled;card.hidden=!enabled;if(!enabled&&selectedDestination==='telegram'){selectedDestination='local';setSelected('destinationCards','destination','local');updateSmartAdd()}}}
qsa('#destinationCards [data-destination]').forEach(b=>b.onclick=()=>{if(b.disabled)return;selectedDestination=b.dataset.destination;setSelected('destinationCards','destination',selectedDestination);updateSmartAdd()});
qsa('#categoryCards [data-category]').forEach(b=>b.onclick=()=>{selectedCategory=b.dataset.category;setSelected('categoryCards','category',selectedCategory)});
qsa('#ytdlpKindCards [data-ytdlp-kind]').forEach(b=>b.onclick=()=>{selectedYtdlpKind=b.dataset.ytdlpKind;selectedYtdlpQuality=selectedYtdlpKind==='audio'?'320':'1080';qsa('#ytdlpKindCards button').forEach(x=>x.classList.toggle('active',x===b));qsa('#videoQualityCards button').forEach(x=>x.classList.toggle('active',x.dataset.ytdlpQuality==='1080'));qsa('#audioQualityCards button').forEach(x=>x.classList.toggle('active',x.dataset.ytdlpQuality==='320'));updateYtdlpQuality()});
qsa('#videoQualityCards [data-ytdlp-quality],#audioQualityCards [data-ytdlp-quality]').forEach(b=>b.onclick=()=>{selectedYtdlpQuality=b.dataset.ytdlpQuality;const group=b.closest('.qualitygrid');[...group.querySelectorAll('button')].forEach(x=>x.classList.toggle('active',x===b));updateYtdlpQuality()});
qs('#link').addEventListener('input',updateSmartAdd);
qs('#uploadFiles').addEventListener('change',updateUploadMeta);
qs('#zip').addEventListener('change',()=>{if(qs('#zip').checked)qs('#extract').checked=false});
qs('#extract').addEventListener('change',()=>{if(qs('#extract').checked)qs('#zip').checked=false});
async function refresh(){try{const s=await api('/api/state');lastState=s;const jellyfinUrl=appUrl(s.stats.jellyfin_url);qs('#stats').innerHTML=metric('CPU',s.stats.cpu+'%')+metric('RAM',s.stats.ram+'%')+metric('Free',s.stats.disk_free)+metric('Tasks',s.stats.tasks);const jf=s.stats.jellyfin;qs('#jfState').textContent=`Jellyfin ${jf.health}`;qs('#jfDot').className='dot '+(jf.running?(jf.health==='healthy'?'':'warn'):'off');qs('#tgState').textContent=s.stats.telegram_ui?'Telegram on':'Telegram off';syncTelegramOptions(s.stats.telegram_ui);qsa('#jellyfinLink,#jellyfinLink2').forEach(a=>a.href=jellyfinUrl);qs('#homeRecent').innerHTML=s.recent.slice(0,5).map(t=>taskHtml(t,true)).join('')||'<div class="empty">No recent activity</div>';qs('#active').innerHTML=s.active.map(t=>taskHtml(t)).join('')||'<div class="empty">No active tasks</div>';qs('#recent').innerHTML=s.recent.slice(0,12).map(t=>taskHtml(t,true)).join('')||'<div class="empty">No recent tasks</div>';qs('#jellyfinSummary').innerHTML=`<div class="pill"><span class="${qs('#jfDot').className}"></span>${esc(jf.state)} / ${esc(jf.health)}</div><div class="muted">URL: <a href="${esc(jellyfinUrl)}" target="_blank">${esc(jellyfinUrl)}</a></div>`}catch(e){console.error(e)}}
async function addLink(){try{const data={link:qs('#link').value,...smartPayload()};await api('/api/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});qs('#link').value='';updateSmartAdd();toast('Task added');showView('status')}catch(e){toast(e.message)}}
async function submitUpload(){try{const form=qs('#uploadForm');const data=new FormData(form);const payload=smartPayload();data.set('destination',payload.destination);data.set('category',payload.category);data.set('name',payload.name);if(payload.zip)data.set('zip','1');if(payload.extract)data.set('extract','1');if(payload.zip_password)data.set('zip_password',payload.zip_password);if(payload.extract_password)data.set('extract_password',payload.extract_password);await api('/api/upload',{method:'POST',body:data});form.reset();updateUploadMeta();toast('Upload task added');showView('status')}catch(err){toast(err.message)}}
async function submitSmartAdd(){if(addMode==='upload')return submitUpload();return addLink()}
qs('#uploadForm').onsubmit=async e=>{e.preventDefault();await submitUpload()};
updateSmartAdd();
updateYtdlpQuality();
async function cancelTask(id){await api('/api/cancel/'+id,{method:'POST'});refresh()}async function cancelAll(){await api('/api/cancelall',{method:'POST'});refresh()}
async function openLocal(){try{const r=await api('/api/local',{method:'POST'});window.open(appUrl(r.url),'_blank')}catch(e){toast(e.message)}}
async function jellyfin(action,target='adminOut'){try{const r=await api('/api/jellyfin/'+action,{method:'POST'});qs('#'+target).textContent=JSON.stringify(r,null,2);toast('Jellyfin action sent');refresh()}catch(e){qs('#'+target).textContent=e.message;toast(e.message)}}
function linkOut(text,url){return `${esc(text)}\n<a href="${esc(url)}" target="_blank">${esc(url)}</a>`}
async function driveSearch(){try{const r=await api('/api/drive/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:qs('#driveQuery').value})});qs('#driveOut').innerHTML=r.url?linkOut(`Found ${r.count} result(s)`,r.url):'No results'}catch(e){qs('#driveOut').textContent=e.message}}
async function driveShare(){try{const r=await api('/api/drive/share',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({link:qs('#shareLink').value})});qs('#driveOut').innerHTML=linkOut(`${r.name}\n${r.files} files / ${r.folders} folders`,r.url)}catch(e){qs('#driveOut').textContent=e.message}}
async function driveDelete(){if(!confirm('Delete this Drive item permanently?'))return;try{qs('#driveOut').textContent=JSON.stringify(await api('/api/drive/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:qs('#deleteDrive').value})}),null,2)}catch(e){qs('#driveOut').textContent=e.message}}
async function driveStats(){try{qs('#driveOut').textContent=JSON.stringify(await api('/api/drive/stats'),null,2)}catch(e){qs('#driveOut').textContent=e.message}}
async function speedtest(){qs('#adminOut').textContent='Running speedtest...';try{qs('#adminOut').textContent=JSON.stringify(await api('/api/speedtest',{method:'POST'}),null,2)}catch(e){qs('#adminOut').textContent=e.message}}
function logs(){window.open('/api/logs','_blank')}async function restart(){if(confirm('Restart Mirror-Bot?'))await api('/api/restart',{method:'POST'})}
const start=(location.hash||'#home').slice(1);if(qs('#'+start))showView(start);setInterval(refresh,3000);refresh();
</script></body></html>'''
