"""The workspace a job runs for, resolved from its payload.

A payload names a workspace (``workspace_id``, else ``DEFAULT_WORKSPACE`` or
``TARGET`` from the environment); the config comes from
``compass_core.config.workspace_config``. This is the open counterpart of the
product's multi-tenant ``WorkspaceContext``; the captains read the same
attributes from either.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from compass_core.config import workspace_config


@dataclass
class Workspace:
    workspace_id: str
    org_id: Optional[str] = None
    user_id: Optional[str] = None
    config: Dict[str, Any] = field(default_factory=dict)


def default_workspace_id() -> Optional[str]:
    """DEFAULT_WORKSPACE or TARGET; else the workspace named in the COMPASS_CONFIG file."""
    explicit = os.getenv("DEFAULT_WORKSPACE") or os.getenv("TARGET")
    if explicit:
        return explicit
    path = os.getenv("COMPASS_CONFIG")
    if path and os.path.isfile(os.path.expanduser(path)):
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and data.get("workspace_id"):
                return str(data["workspace_id"])
        except (OSError, ValueError):
            return None
    return None


def resolve(payload: Dict[str, Any]) -> Workspace:
    workspace_id = payload.get("workspace_id") or default_workspace_id()
    if not workspace_id:
        raise LookupError("workspace_id is required (payload, DEFAULT_WORKSPACE, TARGET, or the COMPASS_CONFIG file)")
    workspace_id = str(workspace_id)
    config = dict(workspace_config(workspace_id) or {})
    org_id = payload.get("org_id") or config.get("org_id") or config.get("org")
    user_id = payload.get("user_id") or config.get("user_id")
    return Workspace(
        workspace_id=workspace_id,
        org_id=str(org_id) if org_id else None,
        user_id=str(user_id) if user_id else None,
        config=config,
    )


def resolve_target(payload: Dict[str, Any], workspace: Workspace) -> str:
    """The target name a captain logs and scopes by: payload, config, TARGET, else the workspace id."""
    for candidate in (payload.get("target"), workspace.config.get("target"), workspace.config.get("name"), os.getenv("TARGET")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return workspace.workspace_id


def captain_config(payload: Dict[str, Any], workspace: Workspace, *, target: Optional[str] = None) -> Dict[str, Any]:
    """The config a captain receives: the workspace config plus the ids every captain reads."""
    config = dict(workspace.config)
    config["workspace_id"] = workspace.workspace_id
    config["org_id"] = payload.get("org_id") or workspace.org_id or config.get("org_id") or config.get("org") or workspace.workspace_id
    if target is not None:
        config["target"] = target
    return config
