import asyncio
import html
import logging
import secrets
from dataclasses import dataclass
from pathlib import PurePosixPath

from aiohttp import web

from ..core.formatting import human_size
from .qbittorrent import QBittorrentClient
from .torrent_selector_page import render_selection_page

LOGGER = logging.getLogger(__name__)


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
    rows.extend(
        (
            "<li class='file'>"
            f"<label class='row' style='--depth:{depth}'>"
            f"<span class='spacer'></span><input class='file-check' type='checkbox' name='file' value='{file['index']}'>"
            f"<span class='name'>{html.escape(file['label'])}</span>"
            f"<small>{human_size(file.get('size', 0))}</small>"
            "</label></li>"
        )
        for file in sorted(node["files"], key=lambda item: item["label"].lower())
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
        self.selections: dict[str, Selection] = {}
        self.selections_by_hash: dict[str, Selection] = {}
        self.runner: web.AppRunner | None = None

    async def select(self, torrent_hash: str, files: list[dict]) -> str:
        token = secrets.token_urlsafe(32)
        selection = Selection(
            token,
            torrent_hash,
            files,
            asyncio.Event(),
            asyncio.Event(),
        )
        async with self.lock:
            await self._start_server()
            self.selections[token] = selection
            self.selections_by_hash[torrent_hash] = selection
        url = f"{self.public_base_url}/select/{token}"
        LOGGER.info("Torrent selector opened hash=%s", torrent_hash[:8])
        try:
            await asyncio.wait_for(selection.submitted.wait(), timeout=self.timeout)
            if selection.cancelled:
                raise asyncio.CancelledError()
            return url
        except TimeoutError as exc:
            raise TimeoutError("Torrent file selection timed out") from exc
        finally:
            await self._drop_selection(selection)
            selection.closed.set()
            LOGGER.info("Torrent selector closed hash=%s", torrent_hash[:8])

    async def get(self, torrent_hash: str) -> Selection | None:
        async with self.lock:
            return self.selections_by_hash.get(torrent_hash)

    async def cancel(self, torrent_hash: str) -> None:
        selection = self.selections_by_hash.get(torrent_hash)
        if selection and selection.torrent_hash == torrent_hash:
            selection.cancelled = True
            selection.submitted.set()
            try:
                await asyncio.wait_for(selection.closed.wait(), timeout=5)
            except TimeoutError:
                await self._drop_selection(selection)

    async def cancel_all(self) -> None:
        for selection in list(self.selections.values()):
            await self.cancel(selection.torrent_hash)

    async def _start_server(self) -> None:
        if self.runner:
            return
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

    async def _drop_selection(self, selection: Selection) -> None:
        async with self.lock:
            self.selections.pop(selection.token, None)
            if self.selections_by_hash.get(selection.torrent_hash) is selection:
                self.selections_by_hash.pop(selection.torrent_hash, None)
            if not self.selections:
                await self._stop_server()

    def _selection(self, request: web.Request) -> Selection:
        token = request.match_info.get("token", "")
        selection = self.selections.get(token)
        if selection is None or not secrets.compare_digest(token, selection.token):
            raise web.HTTPNotFound()
        return selection

    async def _show(self, request: web.Request) -> web.Response:
        selection = self._selection(request)
        rows = render_tree(build_tree(selection.files))
        return web.Response(text=render_selection_page(rows), content_type="text/html")

    async def _submit(self, request: web.Request) -> web.Response:
        selection = self._selection(request)
        form = await request.post()
        if form.get("action") == "cancel":
            selection.cancelled = True
            selection.submitted.set()
            return web.Response(
                text="Torrent cancelled. You can close this page.",
                content_type="text/plain",
            )
        all_ids = {file["index"] for file in selection.files}
        selected = {
            int(value) for value in form.getall("file", []) if value.isdecimal()
        } & all_ids
        if not selected:
            return web.Response(
                text="Select at least one file.", status=400, content_type="text/plain"
            )
        skipped = [file_id for file_id in all_ids if file_id not in selected]
        await self.qb.set_file_priority(selection.torrent_hash, skipped, 0)
        await self.qb.set_file_priority(selection.torrent_hash, sorted(selected), 1)
        await self.qb.start(selection.torrent_hash)
        selection.submitted.set()
        return web.Response(
            text="Selection saved. You can close this page.",
            content_type="text/plain",
        )
