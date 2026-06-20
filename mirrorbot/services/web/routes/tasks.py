from aiohttp import web


def register(app: web.Application, dashboard) -> None:
    app.router.add_get("/api/state", dashboard.api_state)
    app.router.add_post("/api/add", dashboard.api_add)
    app.router.add_post("/api/upload", dashboard.api_upload)
    app.router.add_post("/api/cancel/{task_id}", dashboard.api_cancel)
    app.router.add_post("/api/cancelall", dashboard.api_cancel_all)
    app.router.add_post("/api/local", dashboard.api_local)
