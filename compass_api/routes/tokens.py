"""API token management: ``POST/GET /api/auth/tokens``, ``DELETE /api/auth/tokens/{id}``."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from compass_api.auth.principal import SCOPES
from compass_api.auth.tokens import (
    TokenStoreError,
    bootstrap_configured,
    create_token,
    list_tokens,
    revoke_token,
)
from compass_api.routes._workspace import get_workspace_context

router = APIRouter()


class CreateTokenPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    scope: str = Field(default="operate", description="read | operate | admin")


def _ensure_admin(request: Request) -> None:
    """Token calls need admin scope; cookie/ALB sessions keep full access as today."""
    principal = getattr(request.state, "api_principal", None)
    if principal is not None and not principal.has_scope("admin"):
        raise HTTPException(status_code=403, detail="Managing API tokens requires an admin-scoped token")


def _caller_user_id(request: Request) -> Optional[str]:
    principal = getattr(request.state, "api_principal", None)
    if principal is not None:
        return principal.user_id
    ctx = get_workspace_context(request)
    return ctx.user_id or (os.getenv("APP_USERNAME") or "").strip() or "dashboard"


@router.get("/api/auth/tokens")
def get_tokens(request: Request) -> Dict[str, Any]:
    _ensure_admin(request)
    ctx = get_workspace_context(request)
    try:
        items: List[Dict[str, Any]] = list_tokens(ctx.workspace_id)
    except TokenStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"items": items, "scopes": list(SCOPES), "bootstrap_configured": bootstrap_configured()}


@router.post("/api/auth/tokens", status_code=201)
def post_token(payload: CreateTokenPayload, request: Request) -> Dict[str, Any]:
    _ensure_admin(request)
    ctx = get_workspace_context(request)
    try:
        created = create_token(
            workspace_id=ctx.workspace_id,
            org_id=ctx.org_id,
            user_id=_caller_user_id(request),
            name=payload.name,
            scope=payload.scope,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TokenStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return created


@router.delete("/api/auth/tokens/{token_id}")
def delete_token(token_id: str, request: Request) -> Dict[str, Any]:
    _ensure_admin(request)
    ctx = get_workspace_context(request)
    principal = getattr(request.state, "api_principal", None)
    if principal is not None and principal.token_id == token_id:
        raise HTTPException(status_code=400, detail="A token cannot revoke itself; use another admin token or the dashboard")
    try:
        revoked = revoke_token(ctx.workspace_id, token_id)
    except TokenStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if revoked is None:
        raise HTTPException(status_code=404, detail="Token not found")
    return revoked
