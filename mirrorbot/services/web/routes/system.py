from aiohttp import web


def register(app: web.Application, dashboard) -> None:
    app.router.add_get("/api/logs", dashboard.api_logs)
    app.router.add_post("/api/speedtest", dashboard.api_speedtest)
    app.router.add_post("/api/restart", dashboard.api_restart)
