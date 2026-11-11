from __future__ import annotations

import os
from dataclasses import replace
from typing import Any, Dict

from fastapi import HTTPException, Request

from compass_api import workspace as api_workspace
from compass_api.auth.principal import resolve_principal
from compass_api.middleware.workspace import _identity_from_headers, get_request_principal, get_request_workspace


def get_workspace_context(request: Request) -> Any:
    ctx = get_request_workspace(request)
    if ctx:
        return ctx

    # A route the middleware skipped (or an app without it): the same resolution, once.
    principal = get_request_principal(request)
    if principal is None:
        requested = request.headers.get("X-Workspace-Id") or request.query_params.get("workspace_id")
        principal = resolve_principal(request, _identity_from_headers(request), requested)
    workspace_id = principal.workspace_id or os.getenv("DEFAULT_WORKSPACE") or os.getenv("TARGET")
    if not workspace_id:
        raise HTTPException(status_code=400, detail="workspace_id is required")
    principal = replace(principal, workspace_id=workspace_id)
    request.state.principal = principal

    try:
        ctx = api_workspace.load(workspace_id, org_id=principal.org_id, user_id=principal.user_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    request.state.workspace_ctx = ctx
    return ctx


def get_workspace_config(request: Request) -> Dict[str, Any]:
    ctx = get_workspace_context(request)
    config = ctx.config
    if not isinstance(config, dict):
        raise HTTPException(status_code=500, detail="Workspace config is invalid")
    return config


def get_workspace_dynamodb_config(request: Request) -> Dict[str, Any]:
    ctx = get_workspace_context(request)
    config = get_workspace_config(request)
    raw = config.get("dynamodb_config") or {}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=500, detail="dynamodb_config is invalid")
    result = dict(raw)
    result.setdefault("workspace_id", ctx.workspace_id)
    if result.get("audit") and "items" not in result:
        result["items"] = result["audit"]
    if "items_key" not in result:
        result["items_key"] = "id"
    return result


def get_workspace_target(request: Request) -> str:
    ctx = get_workspace_context(request)
    config = get_workspace_config(request)
    target = config.get("target") or config.get("name") or ctx.workspace_id
    if isinstance(target, str) and target.strip():
        return target.strip()
    raise HTTPException(status_code=500, detail="Workspace target is not configured")


def save_config(request: Request, config: Dict[str, Any]) -> None:
    try:
        api_workspace.save(get_workspace_context(request), config)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def get_actor_ids(request: Request) -> Dict[str, Any]:
    """Who is acting: the workspace, the org and the user id as always, plus ``principal_id``
    and ``principal`` (the public form: email, name, roles, teams, source). Callers spread
    this into job payloads, so the new keys are only these two: a payload's own ``name`` or
    ``email`` is never overwritten. ``user_id`` stays what it was, so every ``actor=`` works."""
    ctx = get_workspace_context(request)
    actor: Dict[str, Any] = {
        "workspace_id": ctx.workspace_id,
        "org_id": ctx.org_id,
        "user_id": ctx.user_id,
    }
    principal = get_request_principal(request)
    if principal is not None:
        actor["principal_id"] = principal.id
        actor["principal"] = principal.to_public()
    return actor
