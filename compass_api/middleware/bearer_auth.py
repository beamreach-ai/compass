from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from compass_api.auth.scopes import required_scope
from compass_api.auth.tokens import looks_like_token, resolve_token


def get_api_principal(request: Request):
    return getattr(request.state, "api_principal", None)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Authenticate ``Authorization: Bearer cmp_...`` calls and enforce token scopes.

    Runs before the workspace middleware so the token decides the workspace.
    Other bearer schemes are passed through untouched.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        header = request.headers.get("authorization") or ""
        if header[:7].lower() != "bearer ":
            return await call_next(request)
        raw = header[7:].strip()
        if not looks_like_token(raw):
            return await call_next(request)

        principal = resolve_token(raw)
        if principal is None:
            return JSONResponse({"detail": "invalid or revoked API token"}, status_code=401)

        needed = required_scope(request.method, request.url.path)
        if needed and not principal.has_scope(needed):
            return JSONResponse(
                {
                    "detail": f"API token scope '{principal.scope}' is not enough for {request.method} {request.url.path}; requires '{needed}'",
                    "required_scope": needed,
                    "token_scope": principal.scope,
                },
                status_code=403,
            )

        request.state.api_principal = principal
        response = await call_next(request)
        return response
