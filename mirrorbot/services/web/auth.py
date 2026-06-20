import secrets

from aiohttp import web

SESSION_COOKIE = "mirrorbot_session"

PUBLIC_PREFIXES = ("/assets/",)
PUBLIC_PATHS = {"/login", "/logout", "/favicon.ico"}


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def credentials_match(expected_username: str, expected_password: str, username: str, password: str) -> bool:
    return secrets.compare_digest(username, expected_username) and secrets.compare_digest(password, expected_password)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def set_session_cookie(response: web.StreamResponse, request: web.Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=request.secure,
        samesite="Lax",
        max_age=7 * 24 * 60 * 60,
    )
