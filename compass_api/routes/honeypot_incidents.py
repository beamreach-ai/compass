"""Incidents (HP-07): the alerts of one intrusion, their timeline, audits and report."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from captains.honeypot import incidents as inc
from captains.honeypot import report as report_mod
from captains.honeypot import summary as summary_mod
from captains.honeypot.respond import approve_pending_action, respond_to_incident, responder_session_factory
from compass_api.routes import honeypots as hp
from compass_api.routes._app_config import write_guard
from compass_api.routes._workspace import get_actor_ids, get_workspace_config
from compass_core.aws import resolve_target_account_id, resolve_target_region

router = APIRouter(dependencies=[Depends(write_guard())], tags=["honeypot-incidents"])


def _public(item: Dict[str, Any]) -> Dict[str, Any]:
    """Native types, and every documented field present (null when unknown)."""
    out = {f: None for f in inc.TIMELINE_FIELDS}
    out.update({"related_incident_id": None, "correlation": None, "blast_radius": None, "summary": None, "action": None})
    out.update(item)
    out.setdefault("honeytoken_ids", [])
    out.setdefault("alert_ids", [])
    out.setdefault("audit_ids", [])
    out.setdefault("repeat_count", 1)
    return hp._to_native(out)


def _detail(table, pk: str, incident_id: str) -> Dict[str, Any]:
    incident = inc.get_incident(table, pk, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="incident not found")
    return {
        "incident": _public(incident),
        "alerts": [hp._to_native(a) for a in inc.alerts_for_incident(table, pk, incident_id)],
        "audits": [hp._to_native(a) for a in inc.audits_for_incident(table, pk, incident_id)],
    }


@router.get("/api/honeypots/incidents")
def list_incidents(request: Request, limit: int = 50) -> List[Dict[str, Any]]:
    table = hp._resolve_table(request)
    return [_public(i) for i in inc.list_incidents(table, hp._ws_pk(request), min(max(int(limit), 1), 200))]


@router.get("/api/honeypots/incidents/{incident_id}")
def get_incident(request: Request, incident_id: str) -> Dict[str, Any]:
    return _detail(hp._resolve_table(request), hp._ws_pk(request), incident_id)


@router.post("/api/honeypots/incidents/{incident_id}/close")
def close_incident(request: Request, incident_id: str) -> Dict[str, Any]:
    table = hp._resolve_table(request)
    pk = hp._ws_pk(request)
    existing = inc.get_incident(table, pk, incident_id)
    if not existing:
        raise HTTPException(status_code=404, detail="incident not found")
    if not existing.get("closed_at"):
        inc.mark(table, pk, incident_id, closed_at=inc.now_iso())
    return _public(inc.get_incident(table, pk, incident_id) or existing)


def _prior_restarts(table, pk: str, incident: Dict[str, Any]) -> int:
    """Restarts run for earlier attempts, following related_incident_id."""
    total, seen, current = 0, {incident["incident_id"]}, incident.get("related_incident_id")
    while current and current not in seen and len(seen) <= 10:
        seen.add(current)
        total += sum(1 for a in inc.audits_for_incident(table, pk, current) if a.get("action_type") == "ecs_restart_task")
        previous = inc.get_incident(table, pk, current)
        current = (previous or {}).get("related_incident_id")
    return total


def _cached_summary(incident: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    s = incident.get("summary")
    return s if isinstance(s, dict) and s.get("paragraph") else None


@router.post("/api/honeypots/incidents/{incident_id}/summarize")
def summarize(request: Request, incident_id: str, force: bool = False) -> Dict[str, Any]:
    table, pk = hp._resolve_table(request), hp._ws_pk(request)
    incident = inc.get_incident(table, pk, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="incident not found")
    cached = _cached_summary(hp._to_native(incident))
    if force and cached:
        # in demo mode a visitor reads the summary there is; asking again is the operator's (one model call each)
        from compass_api.demo_guard import is_demo_admin

        force = is_demo_admin(request)
    if cached and not force:
        return cached
    facts = summary_mod.build_incident_facts(
        hp._to_native(incident),
        [hp._to_native(a) for a in inc.alerts_for_incident(table, pk, incident_id)],
        [hp._to_native(a) for a in inc.audits_for_incident(table, pk, incident_id)],
    )
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(summary_mod.summarize_incident, facts, invoke=summary_mod.INVOKE)
    try:
        result = future.result(timeout=summary_mod.MODEL_TIMEOUT_SECONDS)
    except FutureTimeout:
        raise HTTPException(status_code=504, detail=f"summary timed out after {summary_mod.MODEL_TIMEOUT_SECONDS}s; try again")
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"model returned an unusable summary: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"summary model call failed: {type(exc).__name__}")
    finally:
        pool.shutdown(wait=False)
    inc.mark(table, pk, incident_id, summary=result)
    return result


@router.get("/api/honeypots/incidents/{incident_id}/report")
def incident_report(request: Request, incident_id: str) -> Dict[str, Any]:
    table, pk = hp._resolve_table(request), hp._ws_pk(request)
    detail = _detail(table, pk, incident_id)
    summary = _cached_summary(detail["incident"])
    markdown = report_mod.render_markdown(
        detail["incident"], detail["alerts"], detail["audits"], summary,
        prior_restarts=_prior_restarts(table, pk, detail["incident"]),
    )
    return {**detail, "summary": summary, "markdown": markdown}


class ApprovePayload(BaseModel):
    action_id: str


def _respond_args(request: Request, *, approver: Optional[str] = None) -> Dict[str, Any]:
    cfg = get_workspace_config(request) or {}
    region = resolve_target_region(cfg) or "us-east-1"
    return {
        "region": region,
        "account_id": resolve_target_account_id(cfg, "respond") or resolve_target_account_id(cfg, "read") or "",
        "session_factory": responder_session_factory(cfg, region, approver=approver),
    }


@router.post("/api/honeypots/incidents/{incident_id}/approve")
def approve_action(request: Request, incident_id: str, body: ApprovePayload) -> Dict[str, Any]:
    """Run one action that the gate parked for approval (mode `approve`)."""
    table = hp._resolve_table(request)
    pk = hp._ws_pk(request)
    actor = get_actor_ids(request)
    approved_by = str(actor.get("user_id") or actor.get("email") or "user")
    try:
        return approve_pending_action(
            table=table, pk=pk, incident_id=incident_id, action_id=body.action_id,
            approved_by=approved_by, **_respond_args(request, approver=approved_by),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/honeypots/incidents/{incident_id}/respond")
def respond_now(request: Request, incident_id: str) -> Dict[str, Any]:
    """Re-run choose → gate → run for an incident (e.g. after enabling an action)."""
    table = hp._resolve_table(request)
    pk = hp._ws_pk(request)
    try:
        return respond_to_incident(table=table, pk=pk, incident_id=incident_id, force=True, **_respond_args(request))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
