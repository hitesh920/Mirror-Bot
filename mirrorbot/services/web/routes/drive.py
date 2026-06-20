from aiohttp import web


def register(app: web.Application, dashboard) -> None:
    app.router.add_post("/api/drive/search", dashboard.api_drive_search)
    app.router.add_post("/api/drive/share", dashboard.api_drive_share)
    app.router.add_post("/api/drive/delete", dashboard.api_drive_delete)
    app.router.add_get("/api/drive/stats", dashboard.api_drive_stats)
