import asyncio
import logging
import os
import secrets
import signal
from pathlib import Path
from shutil import rmtree

import psutil
from aiohttp import web

from ..core.config import Config
from ..core.logging_config import create_log_export, log_event
from ..core.models import AddOptions, Destination, Source, SourceType, Task, TaskPhase
from ..core.source_detector import detect_source
from ..downloaders.gdrive import drive_id_from_url
from .drive_sharing import DriveShareError, build_drive_share
from .google_drive_delivery import delete_drive_item, drive_storage_quota, load_credentials, search_drive_items
from .jellyfin import JellyfinControlError, JellyfinManager
from .jellyfin_api import JellyfinApi
from .public_url import public_base_url
from .speedtest import SpeedtestError, run_speedtest
from .status import human_size
from .task_manager import TaskManager
from .web.auth import SESSION_COOKIE, credentials_match, is_public_path, new_session_token, set_session_cookie
from .web.routes import register_dashboard_routes
from .web.serializers import task_json

LOGGER = logging.getLogger(__name__)
FRONTEND_DIR = Path(__file__).resolve().parents[1] / "web_dist"
FRONTEND_INDEX = FRONTEND_DIR / "index.html"
FRONTEND_FALLBACK_PAGE = """<!doctype html>
<html>
  <head>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Mirror-Bot</title>
  </head>
  <body style="font-family:system-ui,sans-serif;padding:32px;line-height:1.5">
    <h1>Mirror-Bot dashboard is not built</h1>
    <p>Run <code>npm install</code> and <code>npm run build</code> inside <code>web/</code>, or rebuild the Docker image.</p>
  </body>
</html>"""
LOGIN_PAGE = """<!doctype html>
<html>
  <head>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Mirror-Bot login</title>
    <style>
      :root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
      body { margin:0; min-height:100vh; display:grid; place-items:center; background:#0b111a; color:#e8eef8; }
      form { width:min(360px, calc(100vw - 40px)); padding:24px; border:1px solid #243244; border-radius:14px; background:#111a26; box-shadow:0 24px 80px rgba(0,0,0,.35); }
      h1 { margin:0 0 6px; font-size:24px; }
      p { margin:0 0 22px; color:#9dafc6; }
      label { display:block; margin:14px 0 7px; color:#b9c7da; font-size:13px; }
      input { width:100%; box-sizing:border-box; padding:12px 13px; border-radius:10px; border:1px solid #2b3b50; background:#0b111a; color:#e8eef8; font:inherit; }
      button { width:100%; margin-top:20px; padding:12px 14px; border:0; border-radius:10px; background:#3478f6; color:white; font-weight:700; cursor:pointer; }
      .error { color:#ff9b9b; margin-top:12px; min-height:18px; }
    </style>
  </head>
  <body>
    <form method="post" action="/login">
      <h1>Mirror-Bot</h1>
      <p>Sign in to continue.</p>
      <label>Username</label>
      <input name="username" autocomplete="username" autofocus>
      <label>Password</label>
      <input name="password" type="password" autocomplete="current-password">
      <button type="submit">Sign in</button>
      <div class="error">__ERROR__</div>
    </form>
  </body>
</html>"""


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
        jellyfin_scan_callback=None,
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
        self.jellyfin_scan_callback = jellyfin_scan_callback
        self.sessions: set[str] = set()
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.speedtest_lock = asyncio.Lock()

    async def start(self) -> None:
        app = web.Application(client_max_size=8 * 1024**3, middlewares=[self.auth_middleware])
        assets_dir = FRONTEND_DIR / "assets"
        register_dashboard_routes(app, self, assets_dir)
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
        path = request.path
        if is_public_path(path):
            return await handler(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        if token and token in self.sessions:
            return await handler(request)
        if path.startswith("/api/"):
            raise web.HTTPUnauthorized(text="Authentication required")
        raise web.HTTPFound("/login")

    async def login_page(self, request: web.Request) -> web.Response:
        if request.cookies.get(SESSION_COOKIE, "") in self.sessions:
            raise web.HTTPFound("/")
        return web.Response(text=LOGIN_PAGE.replace("__ERROR__", ""), content_type="text/html")

    async def login(self, request: web.Request) -> web.Response:
        form = await request.post()
        username = str(form.get("username") or "")
        password = str(form.get("password") or "")
        if not credentials_match(self.config.web_username, self.config.web_password, username, password):
            log_event(LOGGER, logging.WARNING, "web.login", result="failed")
            return web.Response(text=LOGIN_PAGE.replace("__ERROR__", "Invalid username or password."), content_type="text/html", status=401)
        token = new_session_token()
        self.sessions.add(token)
        response = web.HTTPFound("/")
        set_session_cookie(response, request, token)
        log_event(LOGGER, logging.INFO, "web.login", result="success")
        raise response

    async def logout(self, request: web.Request) -> web.Response:
        token = request.cookies.get(SESSION_COOKIE, "")
        self.sessions.discard(token)
        response = web.HTTPFound("/login")
        response.del_cookie(SESSION_COOKIE)
        return response

    async def index(self, request: web.Request) -> web.Response:
        if FRONTEND_INDEX.exists():
            return web.FileResponse(FRONTEND_INDEX)
        return web.Response(text=FRONTEND_FALLBACK_PAGE, content_type="text/html")

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
            "active": [task_json(task, self.completion_payload) for task in active],
            "recent": [task_json(task, self.completion_payload) for task in recent],
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
        if destination == "gdrive":
            destination = Destination.GOOGLE_DRIVE.value
        try:
            return Destination(destination)
        except ValueError as exc:
            raise web.HTTPBadRequest(text="Invalid destination") from exc

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
                if self.jellyfin_scan_callback:
                    result = await self.jellyfin_scan_callback()
                else:
                    result = await asyncio.to_thread(self.jellyfin_api.scan_library)
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
