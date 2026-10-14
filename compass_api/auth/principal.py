from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

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
