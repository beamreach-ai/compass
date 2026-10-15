"""Refuse work for a captain the workspace cannot run (docs/captain_extensions_spec.md §2.3).

A request under a captain's declared route prefixes is checked against the
workspace's captain states: reads pass untouched (a tab opened from an old
URL degrades to an empty view, never an error page); any other method is
answered with 403 ``captain_disabled`` or 402 ``captain_unlicensed`` before
the route runs. The check is by path prefix, not per router, because a router
may serve two captains' paths (the maps router carries ``/api/iac``).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from compass_core import captains

logger = logging.getLogger("compass.api.captain_gate")

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def status_for(exc: captains.CaptainUnavailable) -> int:
    return 402 if exc.code == "captain_unlicensed" else 403


def refusal(exc: captains.CaptainUnavailable) -> JSONResponse:
    return JSONResponse(status_code=status_for(exc), content={"detail": str(exc), **exc.to_dict()})


def _workspace_config(request: Request) -> Optional[Any]:
    ctx = getattr(request.state, "workspace_ctx", None)
    if ctx is None:
        from fastapi import HTTPException

        from compass_api.routes._workspace import get_workspace_context

        try:
            ctx = get_workspace_context(request)
        except HTTPException:
            return None  # the route answers for the missing workspace
    config = getattr(ctx, "config", None)
    return config if isinstance(config, dict) else None


class CaptainGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if request.method in _READ_METHODS:
            return await call_next(request)
        owner = captains.captain_for_path(request.url.path)
        if owner is None:
            return await call_next(request)
        config = _workspace_config(request)
        if config is None:
            return await call_next(request)
        try:
            captains.ensure_active(owner.name, config)
        except captains.CaptainUnavailable as exc:
            return refusal(exc)
        return await call_next(request)
