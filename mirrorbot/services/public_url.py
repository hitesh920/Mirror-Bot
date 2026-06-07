import logging
import socket
from functools import lru_cache
from urllib.parse import urlparse, urlunparse
from urllib.request import urlopen

LOGGER = logging.getLogger(__name__)

_IP_SERVICES = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)


@lru_cache(maxsize=1)
def detect_public_host() -> str:
    for service in _IP_SERVICES:
        try:
            with urlopen(service, timeout=3) as response:
                host = response.read().decode("utf-8", "replace").strip()
            if host:
                LOGGER.info("Detected public host %s using %s", host, service)
                return host
        except Exception as exc:
            LOGGER.debug("Public host detection failed via %s: %s", service, exc)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2)
            sock.connect(("8.8.8.8", 80))
            host = sock.getsockname()[0]
        LOGGER.warning("Using local network host %s for public web links", host)
        return host
    except OSError as exc:
        LOGGER.warning("Could not detect public host, falling back to localhost: %s", exc)
        return "localhost"


def _format_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def public_base_url(port: int, override: str = "") -> str:
    if override:
        parsed = urlparse(override)
        host = parsed.hostname or override.split(":", 1)[0].strip("/")
        scheme = parsed.scheme or "http"
        netloc = f"{_format_host(host)}:{port}"
        if parsed.username:
            auth = parsed.username
            if parsed.password:
                auth += f":{parsed.password}"
            netloc = f"{auth}@{netloc}"
        return urlunparse((scheme, netloc, parsed.path.rstrip("/"), "", "", "")).rstrip("/")

    return f"http://{_format_host(detect_public_host())}:{port}"
