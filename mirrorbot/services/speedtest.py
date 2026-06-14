import asyncio
import json
from dataclasses import dataclass


class SpeedtestError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpeedtestResult:
    ping_ms: float
    download_mbps: float
    upload_mbps: float
    server: str
    sponsor: str
    isp: str


async def run_speedtest(timeout: int = 120) -> SpeedtestResult:
    process = await asyncio.create_subprocess_exec(
        "speedtest-cli",
        "--secure",
        "--json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        process.kill()
        await process.communicate()
        raise
    except TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise SpeedtestError("Speed test timed out after two minutes.") from exc
    if process.returncode:
        detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
        message = detail[-1] if detail else "Speed test service is unavailable."
        raise SpeedtestError(message[:300])
    try:
        result = json.loads(stdout)
        server = result.get("server", {})
        client = result.get("client", {})
        return SpeedtestResult(
            ping_ms=float(result["ping"]),
            download_mbps=float(result["download"]) / 1_000_000,
            upload_mbps=float(result["upload"]) / 1_000_000,
            server=str(server.get("name", "Unknown")),
            sponsor=str(server.get("sponsor", "Unknown")),
            isp=str(client.get("isp", "Unknown")),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SpeedtestError("Speed test returned an invalid response.") from exc
