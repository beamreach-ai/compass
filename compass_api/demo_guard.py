"""Demo mode: the operator's settings are the operator's (docs/demo.md, "Who may change what").

A public demo lets strangers sign in, and a signed-in session is not scoped (API tokens
are: compass_api.auth.scopes). So in demo mode every request a visitor could use to change
how the demo works, reveal a decoy's key or spend the model budget in bulk is refused with
403 ``demo_admin_only`` unless it comes from one of ``demo.admins`` (email addresses, from
the identity the sign-in proxy vouches for) or from an API token with the admin scope.
Everything a visitor does in the tour stays open: the map, the import pull request, a
decoy, "Trip it", the incident.

What counts as an operator's request: everything an API token needs ``admin`` for, the
prefixes and patterns below, and whatever a larger deployment adds with
``extensions.register_demo_admin_prefixes``. Outside demo mode this does nothing.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Dict, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from compass_api import extensions
from compass_api.auth.scopes import required_scope
from compass_core import demo

logger = logging.getLogger("compass.api.demo_guard")

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# writes that change how the demo runs, beyond what an API token needs admin for
_ADMIN_WRITE_PREFIXES = (
    "/api/settings",
    "/api/captains",
    "/api/notifications",
    "/api/updates",
    "/api/auth/tokens",
    "/api/honeypots/autopilot",
    "/api/honeypots/audits",
    "/api/maps/reindex",
    "/api/iac/scan",
    "/api/iac/fix-drift",
    "/api/prs/",
)
_ADMIN_WRITE_PATTERNS = (
    # the incident stays open for the next visitor; responses run on autopilot under their grants
    re.compile(r"^/api/honeypots/incidents/[^/]+/(close|approve|respond)$"),
    # the pre-armed decoys: changed, deleted, re-armed or resolved only by the operator
    re.compile(r"^/api/honeypots/tokens/[^/]+$"),
    re.compile(r"^/api/honeypots/tokens/[^/]+/(arm|execute-runbook|placement)$"),
    re.compile(r"^/api/honeypots/tokens/[^/]+/alerts/[^/]+/resolve$"),
)
_ADMIN_WRITE_METHODS_ON_TOKEN = frozenset({"PUT", "PATCH", "DELETE"})
# reads that hand out a decoy's key or its lure
_ADMIN_READ_SUFFIXES = ("/material", "/lure")


def request_email(request: Request) -> Optional[str]:
    """The signed-in user's address from the ALB's ``x-amzn-oidc-data`` claims, and only when
    the deployment registered the ALB's identity headers (the ALB then sets them on every
    request it passes on). Never ``APP_USERNAME``: on a demo that is everyone's login, so an
    install without a sign-in proxy has no demo admin in the dashboard and is changed with
    the CLI or an admin API token."""
    if not any(name.lower().startswith("x-amzn-oidc") for name in extensions.identity_headers()):
        return None  # no sign-in proxy vouches for the headers: anyone could send them
    if request.headers.get("authorization"):
        return None  # token calls skip the proxy's sign-in (the ALB's bearer rule): its headers are the caller's own
    oidc = request.headers.get("x-amzn-oidc-data", "")
    email = ""
    if oidc.count(".") >= 2:
        try:
            part = oidc.split(".")[1]
            part += "=" * (-len(part) % 4)
            email = str(json.loads(base64.urlsafe_b64decode(part)).get("email") or "").strip()
        except Exception:  # noqa: BLE001
            email = ""
    return email.lower() or None


def admins(config: Optional[Dict[str, Any]]) -> set:
    return {str(a).strip().lower() for a in (demo.settings(config or {}).get("admins") or []) if str(a).strip()}


def is_demo_admin(request: Request, config: Optional[Dict[str, Any]] = None) -> bool:
    """True outside demo mode; in it, an admin API token or one of ``demo.admins``."""
    if not demo.enabled():
        return True
    principal = getattr(request.state, "api_principal", None)
    if principal is not None:
        return bool(principal.has_scope("admin"))
    if config is None:
        config = _config(request)
    email = request_email(request)
    return bool(email) and email in admins(config)


def needs_admin(method: str, path: str) -> bool:
    """Whether, in demo mode, this request is the operator's."""
    method = (method or "GET").upper()
    if method in _READ_METHODS:
        return required_scope(method, path) == "admin" or path.endswith(_ADMIN_READ_SUFFIXES)
    if required_scope(method, path) == "admin":
        return True
    if path.startswith(_ADMIN_WRITE_PREFIXES + extensions.demo_admin_prefixes()):
        return True
    for pattern in _ADMIN_WRITE_PATTERNS:
        if pattern.match(path):
            # a visitor may still create a decoy (POST /api/honeypots/tokens) and trip one
            return not (pattern.pattern == r"^/api/honeypots/tokens/[^/]+$" and method not in _ADMIN_WRITE_METHODS_ON_TOKEN)
    return False


def _config(request: Request) -> Optional[Dict[str, Any]]:
    ctx = getattr(request.state, "workspace_ctx", None)
    config = getattr(ctx, "config", None)
    return config if isinstance(config, dict) else None


class DemoAdminMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if not demo.enabled() or not needs_admin(request.method, request.url.path):
            return await call_next(request)
        if is_demo_admin(request):
            return await call_next(request)
        logger.info("demo: refused %s %s to a visitor", request.method, request.url.path)
        return JSONResponse(status_code=403, content={
            "detail": "In the demo only its operator can do this. Run Compass yourself to change everything.",
            "code": "demo_admin_only",
        })
