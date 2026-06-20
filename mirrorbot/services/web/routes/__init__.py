from aiohttp import web


def register_dashboard_routes(app: web.Application, dashboard, assets_dir) -> None:
    from . import drive, jellyfin, system, tasks

    if assets_dir.exists():
        app.router.add_static("/assets", assets_dir, append_version=False)
    app.router.add_get("/", dashboard.index)
    app.router.add_get("/login", dashboard.login_page)
    app.router.add_post("/login", dashboard.login)
    app.router.add_post("/logout", dashboard.logout)
    tasks.register(app, dashboard)
    drive.register(app, dashboard)
    jellyfin.register(app, dashboard)
    system.register(app, dashboard)
