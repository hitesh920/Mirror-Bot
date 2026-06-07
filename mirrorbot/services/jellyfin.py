import json
import logging
import re
import socket
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

_CONTAINER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


class JellyfinControlError(RuntimeError):
    pass


@dataclass(frozen=True)
class JellyfinStatus:
    name: str
    state: str
    image: str
    health: str
    running: bool


class JellyfinManager:
    def __init__(self, container_name: str, socket_path: str = "/var/run/docker.sock"):
        if not _CONTAINER_RE.fullmatch(container_name):
            raise ValueError("Invalid Jellyfin container name")
        self.container_name = container_name
        self.socket_path = socket_path

    def status(self) -> JellyfinStatus:
        payload = self._request("GET", f"/containers/{self.container_name}/json")
        state = payload.get("State", {})
        health = state.get("Health", {}).get("Status", "unavailable")
        return JellyfinStatus(
            name=self.container_name,
            state=str(state.get("Status") or "unknown"),
            image=str(payload.get("Config", {}).get("Image") or "unknown"),
            health=str(health),
            running=bool(state.get("Running")),
        )

    def start(self) -> JellyfinStatus:
        self._request("POST", f"/containers/{self.container_name}/start", allow=(204, 304))
        return self.status()

    def stop(self) -> JellyfinStatus:
        self._request("POST", f"/containers/{self.container_name}/stop", allow=(204, 304))
        return self.status()

    def restart(self) -> JellyfinStatus:
        self._request("POST", f"/containers/{self.container_name}/restart", allow=(204,))
        return self.status()

    def ensure_running(self) -> JellyfinStatus:
        status = self.status()
        if status.running:
            return status
        LOGGER.info("Jellyfin container %s is not running, starting it", self.container_name)
        return self.start()

    def _request(self, method: str, path: str, allow: tuple[int, ...] = (200,)):
        raw = self._raw_request(method, path)
        header, _, body = raw.partition(b"\r\n\r\n")
        header_lines = header.splitlines()
        status_line = header_lines[0].decode("utf-8", "replace") if header_lines else ""
        headers = {
            key.strip().lower(): value.strip().lower()
            for line in header_lines[1:]
            if b":" in line
            for key, value in [line.decode("utf-8", "replace").split(":", 1)]
        }
        try:
            status_code = int(status_line.split()[1])
        except (IndexError, ValueError) as exc:
            raise JellyfinControlError(f"Invalid Docker response: {status_line}") from exc
        body = _decode_chunked(body) if headers.get("transfer-encoding") == "chunked" else body
        if status_code not in allow:
            detail = body.decode("utf-8", "replace").strip()
            raise JellyfinControlError(f"Docker API returned {status_code}: {detail or status_line}")
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _raw_request(self, method: str, path: str) -> bytes:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(15)
                client.connect(self.socket_path)
                request = (
                    f"{method} {path} HTTP/1.1\r\n"
                    "Host: docker\r\n"
                    "Content-Length: 0\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("utf-8")
                client.sendall(request)
                chunks = []
                while True:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
        except OSError as exc:
            raise JellyfinControlError(f"Could not reach Docker socket: {exc}") from exc
        return b"".join(chunks)


def _decode_chunked(body: bytes) -> bytes:
    decoded = bytearray()
    position = 0
    while True:
        line_end = body.find(b"\r\n", position)
        if line_end == -1:
            break
        size_text = body[position:line_end].split(b";", 1)[0]
        try:
            size = int(size_text, 16)
        except ValueError:
            break
        position = line_end + 2
        if size == 0:
            break
        decoded.extend(body[position:position + size])
        position += size + 2
    return bytes(decoded)
