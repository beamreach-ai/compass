from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from compass_api import extensions
from compass_api import workspace as api_workspace


def _identity_from_headers(request: Request) -> Optional[str]:
    """The signed-in user's id: a header the deployment registered (an ALB's OIDC headers, which
    the proxy sets on every request it passes on) first, else X-User-Id. In that order, so behind
    the proxy a browser cannot name itself someone else (the demo's per-visitor limits)."""
    for name in extensions.identity_headers() + ("X-User-Id",):
        value = request.headers.get(name)
        if value:
            return value
    return None


class WorkspaceContextMiddleware(BaseHTTPMiddleware):
    """Attach a WorkspaceContext to each FastAPI request."""

    def __init__(
        self,
        app,
        *,
        header_name: str = "X-Workspace-Id",
        optional: bool = False,
        default_workspace: Optional[str] = None,
    ) -> None:
        super().__init__(app)
        self.header_name = header_name
        self.optional = optional
        self.default_workspace = default_workspace or os.getenv("DEFAULT_WORKSPACE") or os.getenv("TARGET")

    _SKIP_PATHS = frozenset({
        "/health", "/readyz", "/livez",
        "/api/auth/login", "/api/auth/logout", "/api/auth/me",
    })

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if request.url.path in self._SKIP_PATHS or request.url.path in extensions.public_paths():
            return await call_next(request)
        principal = getattr(request.state, "api_principal", None)
        if principal is not None:
            # An API token pins the workspace; a conflicting header is refused rather than ignored.
            requested = request.headers.get(self.header_name) or request.query_params.get("workspace_id")
            if requested and requested != principal.workspace_id:
                return JSONResponse(
                    {"detail": f"API token is bound to workspace '{principal.workspace_id}'"},
                    status_code=403,
                )
            user_id = principal.user_id
            workspace_id = principal.workspace_id
            org_id = principal.org_id
        else:
            user_id = _identity_from_headers(request)
            workspace_id = request.headers.get(self.header_name) or request.query_params.get("workspace_id")
            org_id = request.headers.get("X-Org-Id") or request.query_params.get("org_id")

            if api_workspace.uses_user_workspace(user_id):
                workspace_id = user_id
                org_id = None
            elif not workspace_id:
                workspace_id = self.default_workspace

        if not workspace_id:
            if self.optional:
                return await call_next(request)
            return JSONResponse({"detail": "workspace_id is required"}, status_code=400)

        try:
            ctx = api_workspace.load(workspace_id, org_id=org_id, user_id=user_id)
        except api_workspace.lookup_errors() as exc:
            if self.optional:
                request.state.workspace_ctx = None
                request.state.workspace_error = str(exc)
                return await call_next(request)
            return JSONResponse({"detail": str(exc)}, status_code=404)
        request.state.workspace_ctx = ctx
        response: Response = await call_next(request)
        if ctx and hasattr(response, "headers"):
            response.headers.setdefault("X-Workspace-Id", ctx.workspace_id)
        return response


def get_request_workspace(request: Request) -> Optional[Any]:
    return getattr(request.state, "workspace_ctx", None)


def require_workspace(request: Request) -> Any:
    ctx = get_request_workspace(request)
    if ctx is None:
        raise HTTPException(status_code=400, detail="workspace_id is required")
    return ctx


def require_workspace_user(request: Request) -> Any:
    ctx = require_workspace(request)
    if not ctx.user_id:
        raise HTTPException(status_code=401, detail="user_id is required")
    return ctx
