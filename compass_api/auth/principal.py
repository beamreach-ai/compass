"""Who is calling.

``ApiPrincipal`` is the API-token caller, ``Principal`` is any caller: a token, the
signed-in person of a cookie session, the identity a proxy put in a header, or
nobody. The workspace middleware resolves one ``Principal`` per request through
the resolver installed here; a host (``compass_core.hosts``) replaces the resolver
with ``set_principal_resolver`` to fill it from its own directory, with its own
roles and teams. The open default keeps today's behaviour: every signed-in person
is ``admin``, and the identity header a proxy sets is the user id.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import Request

SCOPES = ("read", "operate", "admin")
_SCOPE_RANK = {name: index for index, name in enumerate(SCOPES)}


def normalize_scope(value: Optional[str], default: str = "operate") -> str:
    scope = str(value or "").strip().lower() or default
    if scope not in _SCOPE_RANK:
        raise ValueError(f"scope must be one of: {', '.join(SCOPES)}")
    return scope


def scope_covers(granted: str, required: str) -> bool:
    return _SCOPE_RANK.get(granted, -1) >= _SCOPE_RANK.get(required, 0)


@dataclass(frozen=True)
class Principal:
    """Who is calling, whatever authenticated them.

    ``roles`` is the host's vocabulary; the open default uses the token scopes
    (``admin``, ``operate``, ``read``) so one vocabulary serves tokens and people.
    ``source`` says what authenticated the caller: ``api_token``, ``session`` (the
    cookie), ``header`` (a proxy's identity header), ``anonymous`` (auth is off or
    nobody signed in), or a host's own. ``workspace_id`` is the workspace the
    principal names; None leaves it to the request and the default.
    """

    id: str                              # stable: a user id, a token id, "anonymous"
    kind: str                            # "user" | "token" | "system"
    workspace_id: Optional[str] = None
    org_id: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    roles: Tuple[str, ...] = ()
    teams: Tuple[str, ...] = ()
    source: str = "session"
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def anonymous(self) -> bool:
        return self.source == "anonymous"

    @property
    def user_id(self) -> Optional[str]:
        """What ``ctx.user_id`` has always been: the identity, None for nobody, and for a
        token the user it was minted for (``attributes["user_id"]``), which may be None."""
        if self.anonymous:
            return None
        return self.attributes.get("user_id", self.id)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "workspace_id": self.workspace_id,
            "org_id": self.org_id,
            "email": self.email,
            "name": self.name,
            "roles": list(self.roles),
            "teams": list(self.teams),
            "source": self.source,
        }


@dataclass(frozen=True)
class ApiPrincipal:
    """Who is calling, when the call is authenticated with an API token."""

    token_id: str
    kind: str  # "personal" | "bootstrap"
    scope: str
    workspace_id: str
    org_id: Optional[str] = None
    user_id: Optional[str] = None
    name: Optional[str] = None

    def has_scope(self, required: str) -> bool:
        return scope_covers(self.scope, required)

    def to_public(self) -> dict:
        return {
            "token_id": self.token_id,
            "kind": self.kind,
            "scope": self.scope,
            "workspace_id": self.workspace_id,
            "org_id": self.org_id,
            "user_id": self.user_id,
            "name": self.name,
        }

    def to_principal(self) -> Principal:
        """The token as a principal: its scope is its one role, its user is the id."""
        return Principal(
            id=self.user_id or self.token_id,
            kind="token",
            workspace_id=self.workspace_id,
            org_id=self.org_id,
            name=self.name,
            roles=(self.scope,),
            source="api_token",
            attributes={"token_id": self.token_id, "token_kind": self.kind, "user_id": self.user_id},
        )


# ── the resolver seam ─────────────────────────────────────────────────────────

Resolver = Callable[["Request", Optional[str], Optional[str]], Optional[Principal]]
# (request, user id from the identity headers, requested workspace id) -> Principal,
# or None to fall back to the open default

_resolver: Optional[Resolver] = None


def set_principal_resolver(resolver: Optional[Resolver]) -> None:
    """Install a host's resolver; None restores the open default."""
    global _resolver
    _resolver = resolver


def principal_resolver() -> Optional[Resolver]:
    return _resolver


def _requested_org(request: "Request") -> Optional[str]:
    return request.headers.get("X-Org-Id") or request.query_params.get("org_id")


def default_resolver(request: "Request", user_id: Optional[str], requested_workspace_id: Optional[str]) -> Principal:
    """The open edition: a token, else the proxy's identity header, else the cookie
    session's ``APP_USERNAME``, else nobody. Everyone signed in is ``admin``. The
    workspace is the identity itself when the host says a user's id is their
    workspace (``compass_api.workspace.uses_user_workspace``), else the requested one."""
    from compass_api import workspace as api_workspace
    from compass_api.routes.auth import _auth_enabled, validate_session

    api_principal = getattr(request.state, "api_principal", None)
    if api_principal is not None:
        return api_principal.to_principal()

    if user_id:
        identity: Optional[Principal] = Principal(id=user_id, kind="user", roles=("admin",), source="header")
    elif _auth_enabled() and validate_session(request.cookies.get("compass_session")):
        identity = Principal(id=os.environ.get("APP_USERNAME", "").strip(), kind="user", roles=("admin",), source="session")
    else:
        identity = None

    if identity is not None and api_workspace.uses_user_workspace(identity.user_id):
        return replace(identity, workspace_id=identity.id, org_id=None)
    if identity is None:
        identity = Principal(id="anonymous", kind="user", roles=("admin",), source="anonymous")
    return replace(identity, workspace_id=requested_workspace_id, org_id=_requested_org(request))


def resolve_principal(request: "Request", user_id: Optional[str], requested_workspace_id: Optional[str]) -> Principal:
    """One principal per request: the host's resolver when it answers, else the open default."""
    if _resolver is not None:
        principal = _resolver(request, user_id, requested_workspace_id)
        if principal is not None:
            return principal
    return default_resolver(request, user_id, requested_workspace_id)
