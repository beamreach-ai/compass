"""Response-action whitelist: what Compass may do in the target account when a
honeytoken trips, and the audit of what it did.

Rows are the only thing the agent may choose from (HP-10) and the only source
of the responder role's permissions (``captains.honeypot.responder_role``).
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from boto3.dynamodb.conditions import Key
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from captains.honeypot.actions import ACTION_CATALOG, catalog, get_action
from captains.honeypot.actions.base import ActionScope
from captains.honeypot.policy import MODES
from captains.honeypot.responder_role import DEFAULT_ROLE_NAME, generate_responder_module, simulate_actions
from compass_api.routes import honeypots as hp
from compass_api.routes._app_config import write_guard
from compass_api.routes._workspace import get_actor_ids, get_workspace_config
from compass_core import sessions
from compass_core.aws import resolve_target_account_id, resolve_target_region
from compass_core.keys import (
    honeypot_action_prefix,
    honeypot_action_sort_key,
    honeypot_audit_prefix,
)

router = APIRouter(dependencies=[Depends(write_guard())], tags=["honeypot-actions"])


class ScopePayload(BaseModel):
    service_arns: List[str] = Field(default_factory=list)
    user_arns: List[str] = Field(default_factory=list)
    user_path_prefixes: List[str] = Field(default_factory=list)


class ResponseActionPayload(BaseModel):
    action_type: str
    scope: ScopePayload = Field(default_factory=ScopePayload)
    mode: str = Field(default="approve", pattern="^(off|approve|autopilot)$")
    risk_max: int = Field(default=2, ge=1, le=5)
    enabled: bool = True


class ResponseActionUpdatePayload(BaseModel):
    scope: Optional[ScopePayload] = None
    mode: Optional[str] = Field(default=None, pattern="^(off|approve|autopilot)$")
    risk_max: Optional[int] = Field(default=None, ge=1, le=5)
    enabled: Optional[bool] = None


def _list_rows(table, pk: str) -> List[Dict[str, Any]]:
    resp = table.query(KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(honeypot_action_prefix()))
    return [hp._to_native(_serialize(i)) for i in resp.get("Items", [])]


def _serialize(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "action_id": item.get("action_id"),
        "action_type": item.get("action_type"),
        "scope": ActionScope.from_dict(item.get("scope")).to_dict(),
        "mode": item.get("mode", "approve"),
        "risk_max": int(item.get("risk_max", 2)),
        "enabled": bool(item.get("enabled", True)),
        "created_at": item.get("created_at"),
        "created_by": item.get("created_by"),
        "updated_at": item.get("updated_at"),
        "catalog": get_action(item["action_type"]).describe() if item.get("action_type") in ACTION_CATALOG else None,
    }


def _validate_scope(action_type: str, scope: ScopePayload) -> None:
    action = get_action(action_type)
    wanted = set(action.scope_fields())
    given = {f for f in ("service_arns", "user_arns", "user_path_prefixes") if getattr(scope, f)}
    if wanted and not (wanted & given):
        raise HTTPException(status_code=400, detail=f"{action_type} needs a scope: one of {sorted(wanted)}")


def _responder_config(request: Request) -> Dict[str, Any]:
    cfg = get_workspace_config(request) or {}
    hp_cfg = cfg.get("honeypot") if isinstance(cfg.get("honeypot"), dict) else {}
    responder = hp_cfg.get("responder") if isinstance(hp_cfg.get("responder"), dict) else {}
    return {
        "account_id": responder.get("account_id") or resolve_target_account_id(cfg, "respond") or resolve_target_account_id(cfg, "read") or "",
        "region": responder.get("region") or resolve_target_region(cfg) or "us-east-1",
        "trust_principal_arns": list(responder.get("trust_principal_arns") or []),
        "external_id": responder.get("external_id") or "",
        "role_name": responder.get("role_name") or DEFAULT_ROLE_NAME,
        "workspace_config": cfg,
    }


# ── catalog ────────────────────────────────────────────────────────────────

@router.get("/api/honeypots/actions/catalog")
def get_catalog() -> List[Dict[str, Any]]:
    return catalog()


# ── responder role ──────────────────────────────────────────────────────────

@router.get("/api/honeypots/actions/responder-module")
def responder_module(request: Request) -> Dict[str, Any]:
    """The Terraform the enabled rows translate to. HP-01's writer turns this
    into a PR; here it is previewed."""
    table = hp._resolve_table(request)
    rows = _list_rows(table, hp._ws_pk(request))
    cfg = _responder_config(request)
    if not cfg["trust_principal_arns"]:
        raise HTTPException(
            status_code=400,
            detail="honeypot.responder.trust_principal_arns is not configured (the Compass worker role that assumes the responder)",
        )
    module = generate_responder_module(
        rows,
        account_id=cfg["account_id"],
        region=cfg["region"],
        trust_principal_arns=cfg["trust_principal_arns"],
        external_id=cfg["external_id"] or None,
        role_name=cfg["role_name"],
    )
    module.pop("trust", None)
    return module


@router.post("/api/honeypots/actions/check")
def check_responder(request: Request) -> Dict[str, Any]:
    """Ask IAM whether the applied responder role can do what the enabled rows
    need. Runs against the real target account."""
    table = hp._resolve_table(request)
    rows = _list_rows(table, hp._ws_pk(request))
    cfg = _responder_config(request)
    module = generate_responder_module(
        rows,
        account_id=cfg["account_id"],
        region=cfg["region"],
        trust_principal_arns=cfg["trust_principal_arns"] or ["arn:aws:iam::000000000000:root"],
        external_id=cfg["external_id"] or None,
        role_name=cfg["role_name"],
    )
    try:
        session = sessions.target("act", region=cfg["region"], config=cfg["workspace_config"], approver=get_actor_ids(request).get("user_id"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"cannot assume the responder role: {exc}") from exc
    if session is None:
        raise HTTPException(status_code=400, detail="aws.assume_role_respond is not configured for this workspace")
    iam = session.client("iam", region_name=cfg["region"])
    results = simulate_actions(iam, module["role_arn"], module["policy"])
    denied = [r for r in results if r["decision"] != "allowed"]
    return {"role_arn": module["role_arn"], "ok": not denied, "results": results, "denied": denied}


@router.post("/api/honeypots/actions/responder-pr")
def open_responder_pr(request: Request) -> Dict[str, Any]:
    """Queue the job that opens (or updates) the responder-role PR."""
    from compass_core.queue import job_queue

    cfg = _responder_config(request)
    if not cfg["trust_principal_arns"]:
        raise HTTPException(status_code=400, detail="honeypot.responder.trust_principal_arns is not configured")
    actor = get_actor_ids(request)
    job = job_queue().enqueue(
        captain="honeypot", command="responder_pr",
        payload={"workspace_id": actor.get("workspace_id"), "requested_by": actor.get("user_id") or ""},
    )
    return {"queued": True, "job": job if isinstance(job, (str, dict)) else str(job)}


# ── whitelist rows ──────────────────────────────────────────────────────────

@router.get("/api/honeypots/actions")
def list_actions(request: Request) -> Dict[str, Any]:
    table = hp._resolve_table(request)
    return {"actions": _list_rows(table, hp._ws_pk(request)), "catalog": catalog()}


@router.post("/api/honeypots/actions")
def create_action(request: Request, body: ResponseActionPayload) -> Dict[str, Any]:
    if body.action_type not in ACTION_CATALOG:
        raise HTTPException(status_code=400, detail=f"unknown action_type {body.action_type!r}")
    _validate_scope(body.action_type, body.scope)
    table = hp._resolve_table(request)
    pk = hp._ws_pk(request)
    actor = get_actor_ids(request)
    action_id = f"act-{uuid.uuid4().hex[:12]}"
    now = hp._epoch_ms()
    item = {
        "pk": pk,
        "sk": honeypot_action_sort_key(action_id),
        "action_id": action_id,
        "action_type": body.action_type,
        "scope": body.scope.model_dump(),
        "mode": body.mode,
        "risk_max": body.risk_max,
        "enabled": body.enabled,
        "created_at": now,
        "updated_at": now,
        "created_by": actor.get("user_id") or actor.get("email") or "",
    }
    table.put_item(Item=item)
    return hp._to_native(_serialize(item))


@router.put("/api/honeypots/actions/{action_id}")
def update_action(request: Request, action_id: str, body: ResponseActionUpdatePayload) -> Dict[str, Any]:
    table = hp._resolve_table(request)
    pk = hp._ws_pk(request)
    key = {"pk": pk, "sk": honeypot_action_sort_key(action_id)}
    item = table.get_item(Key=key).get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="response action not found")
    if body.scope is not None:
        _validate_scope(item["action_type"], body.scope)
        item["scope"] = body.scope.model_dump()
    if body.mode is not None:
        item["mode"] = body.mode
    if body.risk_max is not None:
        item["risk_max"] = body.risk_max
    if body.enabled is not None:
        item["enabled"] = body.enabled
    item["updated_at"] = hp._epoch_ms()
    table.put_item(Item=item)
    return hp._to_native(_serialize(item))


@router.delete("/api/honeypots/actions/{action_id}")
def delete_action(request: Request, action_id: str) -> Dict[str, Any]:
    table = hp._resolve_table(request)
    key = {"pk": hp._ws_pk(request), "sk": honeypot_action_sort_key(action_id)}
    if not table.get_item(Key=key).get("Item"):
        raise HTTPException(status_code=404, detail="response action not found")
    table.delete_item(Key=key)
    return {"deleted": action_id}


# ── audit ───────────────────────────────────────────────────────────────────

@router.get("/api/honeypots/audits")
def list_audits(request: Request, limit: int = 20) -> List[Dict[str, Any]]:
    table = hp._resolve_table(request)
    resp = table.query(
        KeyConditionExpression=Key("pk").eq(hp._ws_pk(request)) & Key("sk").begins_with(honeypot_audit_prefix()),
        ScanIndexForward=False,
        Limit=max(1, min(int(limit), 100)),
    )
    out = []
    for item in resp.get("Items", []):
        item = dict(item)
        item.pop("pk", None)
        item.pop("sk", None)
        out.append(hp._to_native(item))
    return out


class RollbackPayload(BaseModel):
    reason: str = Field(default="", max_length=500)


@router.post("/api/honeypots/audits/{audit_id}/rollback")
def rollback_audit(request: Request, audit_id: str, body: RollbackPayload = RollbackPayload()) -> Dict[str, Any]:
    """Undo a verified response action (e.g. re-activate a deactivated key)
    through the responder role, and record who did it on the audit row."""
    from captains.honeypot.actions.base import ActionContext
    from captains.honeypot.responder import AuditLog, rollback_response_action
    from compass_core.keys import honeypot_audit_sort_key

    table = hp._resolve_table(request)
    pk = hp._ws_pk(request)
    item = table.get_item(Key={"pk": pk, "sk": honeypot_audit_sort_key(audit_id)}).get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Audit not found")
    audit = hp._to_native({k: v for k, v in item.items() if k not in ("pk", "sk")})
    try:
        action = get_action(audit.get("action_type", ""))
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"unknown action {audit.get('action_type')!r}") from exc
    if not action.rollback_supported:
        raise HTTPException(status_code=409, detail=f"{action.action_type} cannot be rolled back")
    if audit.get("status") != "verified":
        raise HTTPException(status_code=409, detail=f"only a verified action can be rolled back (this one is {audit.get('status')})")
    if (audit.get("rollback") or {}).get("done_at"):
        raise HTTPException(status_code=409, detail="already rolled back")

    target = audit.get("target") or {}
    user_arn = target.get("user_arn") or ""
    ctx = ActionContext(
        region=target.get("region") or resolve_target_region(get_workspace_config(request)) or "us-east-1",
        incident_id=audit.get("incident_id") or "",
        cluster_arn=target.get("cluster_arn") or "",
        service_arn=target.get("service_arn") or "",
        task_arn=target.get("task_arn") or "",
        container=target.get("container") or "",
        user_arn=user_arn,
        user_name=user_arn.rsplit("/", 1)[-1] if user_arn else "",
        access_key_id=target.get("access_key_id") or "",
    )
    actor = get_actor_ids(request)
    requested_by = actor.get("user_id") or actor.get("workspace_id") or "unknown"
    try:
        session = sessions.target("act", region=ctx.region, config=get_workspace_config(request), approver=actor.get("user_id"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"cannot assume the responder role: {exc}") from exc
    if session is None:
        raise HTTPException(status_code=400, detail="aws.assume_role_respond is not configured for this workspace")
    out = rollback_response_action(
        session=session, audit=audit, ctx=ctx, audit_log=AuditLog(table, pk), requested_by=requested_by, action=action
    )
    if body.reason:
        out["rollback"] = {**(out.get("rollback") or {}), "reason": body.reason}
        AuditLog(table, pk).write(out)
    if (out.get("rollback") or {}).get("error"):
        raise HTTPException(status_code=502, detail=f"rollback failed: {out['rollback']['error']}")
    return out


__all__ = ["router", "MODES"]
