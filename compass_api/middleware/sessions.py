"""Every request runs inside a session context (``compass_core.sessions``): the captain
that owns the path (``core`` for Compass's own routes), the signed-in user as the
requester, and a request id that names the AWS sessions the request makes
(``<prefix>-<captain>-api-<id>``). Innermost, so the workspace middleware has already
put the user on the request."""

from __future__ import annotations

import uuid

from compass_core import captains, sessions


class SessionContextMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        owner = captains.captain_for_path(scope.get("path") or "")
        state = scope.setdefault("state", {})
        workspace_ctx = state.get("workspace_ctx")
        requester = getattr(workspace_ctx, "user_id", None) if workspace_ctx is not None else None
        request_id = uuid.uuid4().hex
        state["request_id"] = request_id
        with sessions.captain_context(owner.name if owner else sessions.CORE, requester=requester or None, request_id=request_id):
            await self.app(scope, receive, send)
