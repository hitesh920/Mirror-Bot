from aiohttp import web


def register(app: web.Application, dashboard) -> None:
    app.router.add_post("/api/jellyfin/{action}", dashboard.api_jellyfin)
