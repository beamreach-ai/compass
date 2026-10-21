"""``/api/access``: the roles, the grants with their states, the verification and the
audit (docs/specs/access-model.md §12). Writes need the admin scope."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from compass_api.routes._app_config import ensure_settings_enabled, write_guard
from compass_api.routes._workspace import get_actor_ids, get_workspace_config, get_workspace_dynamodb_config
from compass_core import access_module, actions, grants
from compass_core.aws import control_config, roles
from compass_core.keys import workspace_pk
from compass_core.tables import dynamo_table, tables

router = APIRouter(dependencies=[Depends(write_guard())])
logger = logging.getLogger("compass.api.access")


def _table(request: Request):
    name = tables(get_workspace_dynamodb_config(request)).items
    if not name:
        raise HTTPException(status_code=500, detail="dynamodb_config.items not configured")
    return dynamo_table(name)


def _pk(request: Request) -> str:
    return workspace_pk(get_actor_ids(request).get("workspace_id"))


def _serialize(row: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    entry = actions.describe_action(str(row.get("action_type") or ""))
    try:
        statements = grants.statements_for(row, config)
    except Exception:  # noqa: BLE001 - an action its captain no longer registers
        statements = []
    return {
        "grant_id": row.get("action_id"), "action_id": row.get("action_id"), "captain": row.get("captain"), "action_type": row.get("action_type"),
        "title": entry.get("title") or row.get("action_type"), "change_type": entry.get("change_type"), "rollback_supported": entry.get("rollback_supported"),
        "scope": row.get("scope") or {}, "mode": row.get("mode"), "risk_max": row.get("risk_max"), "enabled": bool(row.get("enabled", True)),
        "grant_status": row.get("grant_status"), "armed": grants.is_armed(row), "grant_pr": row.get("grant_pr"),
        "applied_at": row.get("applied_at"), "verified_at": row.get("verified_at"), "armed_at": row.get("armed_at"),
        "policy_hash": row.get("policy_hash"), "verification": row.get("verification"), "statements": statements,
        "created_at": row.get("created_at"), "created_by": row.get("created_by"), "accepted_by": row.get("accepted_by"),
        "supersedes": row.get("supersedes"), "superseded_by": row.get("superseded_by"), "history": row.get("grant_history") or [],
    }


@router.get("/api/access")
def get_access(request: Request) -> Dict[str, Any]:
    """The roles, the grants with their states, the last verification and the findings."""
    config = get_workspace_config(request)
    table = _table(request)
    pk = _pk(request)
    rows = grants.list_grants(table, pk)
    aws = config.get("aws") if isinstance(config.get("aws"), dict) else {}
    findings: List[Dict[str, Any]] = []
    audit: Optional[Dict[str, Any]] = None
    try:
        from compass_core import access_audit

        audit = access_audit.last_run(table, pk)
        findings = access_audit.open_findings(table, pk)
    except ImportError:
        pass
    from compass_core.access_pr import repository

    return {
        "connected": bool(roles(config).get("read")),
        "roles": roles(config),
        "runtime_arn": control_config(config).get("runtime_arn"),
        "verified_at": aws.get("verified_at"),
        "verification": aws.get("verification"),
        "repository": repository(config),
        "grants": [_serialize(r, config) for r in rows],
        "summary": grants.summary(table, pk),
        "findings": findings,
        "audit": audit,
        "catalog": actions.catalog(),
    }


class ProposePayload(BaseModel):
    captain: str
    action_type: str
    scope: Dict[str, Any] = Field(default_factory=dict)
    mode: str = "approve"
    risk_max: int = Field(default=2, ge=1, le=5)
    region: Optional[str] = None
    accept: bool = False


@router.post("/api/access/grants")
def propose_grant(request: Request, body: ProposePayload) -> Dict[str, Any]:
    """Propose an action with a scope; with ``accept`` true, accept it in the same step."""
    ensure_settings_enabled()
    config = get_workspace_config(request)
    actor = get_actor_ids(request).get("user_id")
    try:
        row = grants.propose(_table(request), _pk(request), captain=body.captain, action_type=body.action_type, scope=body.scope,
                             mode=body.mode, risk_max=body.risk_max, actor=actor, region=body.region)
        if body.accept and body.mode != "off":
            row = grants.accept(_table(request), _pk(request), str(row["action_id"]), config=config, actor=actor, mode=body.mode)
    except grants.GrantError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize(row, config)


class AcceptPayload(BaseModel):
    mode: Optional[str] = None


@router.post("/api/access/grants/{grant_id}/accept")
def accept_grant(request: Request, grant_id: str, body: Optional[AcceptPayload] = None) -> Dict[str, Any]:
    """A person accepts: the grant is ``pr_open`` and the pull request is queued."""
    ensure_settings_enabled()
    config = get_workspace_config(request)
    try:
        row = grants.accept(_table(request), _pk(request), grant_id, config=config, actor=get_actor_ids(request).get("user_id"), mode=(body.mode if body else None))
    except grants.GrantError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize(row, config)


@router.post("/api/access/grants/{grant_id}/decline")
def decline_grant(request: Request, grant_id: str) -> Dict[str, Any]:
    ensure_settings_enabled()
    try:
        row = grants.decline(_table(request), _pk(request), grant_id, actor=get_actor_ids(request).get("user_id"))
    except grants.GrantError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize(row, get_workspace_config(request))


@router.post("/api/access/grants/{grant_id}/remove")
def remove_grant(request: Request, grant_id: str) -> Dict[str, Any]:
    """Disarmed at once; the pull request that takes the permission away is queued."""
    ensure_settings_enabled()
    config = get_workspace_config(request)
    try:
        row = grants.remove(_table(request), _pk(request), grant_id, config=config, actor=get_actor_ids(request).get("user_id"))
    except grants.GrantError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize(row, config)


@router.post("/api/access/verify")
def verify_grants(request: Request) -> Dict[str, Any]:
    """Read the act role back from IAM and move every in-flight grant as far as it allows."""
    ensure_settings_enabled()
    config = get_workspace_config(request)
    actor = get_actor_ids(request)
    result = grants.refresh(_table(request), _pk(request), config, actor=actor.get("user_id"))
    return {**result, "grants": [_serialize(r, config) for r in grants.list_grants(_table(request), _pk(request))]}


@router.get("/api/access/module")
def access_module_preview(request: Request) -> Dict[str, Any]:
    """The Terraform the grants translate to, as the next pull request would carry it."""
    config = get_workspace_config(request)
    rendered = access_module.render(config, grants.list_grants(_table(request), _pk(request)))
    rendered.pop("trust", None)
    return rendered


@router.post("/api/access/pr")
def open_access_pr(request: Request) -> Dict[str, Any]:
    """Queue the job that opens (or updates) the access pull request."""
    from compass_core.queue import job_queue

    ensure_settings_enabled()
    actor = get_actor_ids(request)
    job = job_queue().enqueue(captain="core", command=grants.PR_COMMAND,
                              payload={"verb": "pr", "workspace_id": actor.get("workspace_id"), "user_id": actor.get("user_id") or "", "reason": "requested from the page"})
    return {"queued": True, "job": job if isinstance(job, (str, dict)) else str(job)}


@router.post("/api/access/audit")
def run_access_audit(request: Request) -> Dict[str, Any]:
    """Run the self-audit now: what the roles hold against what the manifests and grants explain."""
    ensure_settings_enabled()
    try:
        from compass_core import access_audit
    except ImportError as exc:
        raise HTTPException(status_code=501, detail="the self-audit is not available in this build") from exc
    config = get_workspace_config(request)
    return access_audit.run(config, workspace_id=str(get_actor_ids(request).get("workspace_id")), table=_table(request), actor=get_actor_ids(request).get("user_id"))
