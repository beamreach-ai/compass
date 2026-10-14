from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from compass_api import extensions
from compass_api.routes.auth import _auth_enabled, validate_session

_PUBLIC_PATHS = frozenset({
    "/health",
    "/readyz",
    "/livez",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
})

_GATED_PREFIXES = ("/api/",)


class SimpleAuthMiddleware(BaseHTTPMiddleware):
    """Gate /api (and what the product registers) behind a session cookie or an API token when APP_USERNAME/APP_PASSWORD are set."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if not _auth_enabled():
            return await call_next(request)

        path = request.url.path
        if path in _PUBLIC_PATHS or path in extensions.public_paths() or not path.startswith(_GATED_PREFIXES + extensions.gated_prefixes()):
            return await call_next(request)

        if getattr(request.state, "api_principal", None) is not None:
            return await call_next(request)

        token = request.cookies.get("compass_session")
        if not validate_session(token):
            return JSONResponse({"detail": "authentication required"}, status_code=401)

        return await call_next(request)
