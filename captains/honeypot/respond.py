"""HP-10/HP-11: after the investigation, decide and act.

Choose among the whitelisted rows (HP-10), score each with the deterministic
policy and apply the gate (HP-11), run what the gate allows through the
responder (HP-27), record everything on the incident, and tell the channel.
Rows in ``approve`` mode (or above their risk cap) are parked as pending
approvals that the UI can run with one click.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from boto3.dynamodb.conditions import Key

from captains.honeypot import incidents
from captains.honeypot.actions import get_action
from captains.honeypot.actions.base import ActionContext
from captains.honeypot.choose import choose_actions
from captains.honeypot.investigate import region_of_arns
from captains.honeypot.policy import gate, score_action
from captains.honeypot.responder import AuditLog, run_response_action
from compass_core import grants
from compass_core.keys import honeypot_action_prefix, honeypot_action_sort_key

logger = logging.getLogger("honeypot-respond")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_action_rows(table: Any, pk: str) -> List[Dict[str, Any]]:
    resp = table.query(KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(honeypot_action_prefix()))
    return [dict(i) for i in resp.get("Items", [])]


def context_from_incident(incident: Dict[str, Any], alerts: List[Dict[str, Any]], *, region: str, account_id: str = "") -> ActionContext:
    """What the actions need to know about the target, from the investigation
    and the alerts."""
    corr = incident.get("correlation") or {}
    task = corr.get("task") or {}
    placement = incident.get("placement") or {}
    key_alert = next((a for a in alerts if a.get("access_key_id") or a.get("principal_arn")), {})
    principal = str(key_alert.get("principal_arn") or "")
    user_name = principal.rsplit("/", 1)[-1] if ":user/" in principal else ""
    # Act where the target lives, not where the workspace default points.
    region = region_of_arns(task.get("task_arn"), corr.get("service_arn"), incident.get("service_arn"), placement.get("service_arn"), placement.get("cluster_arn")) or region
    return ActionContext(
        region=region,
        incident_id=str(incident.get("incident_id") or ""),
        account_id=account_id,
        cluster_arn=str(task.get("cluster_arn") or incident.get("cluster_arn") or placement.get("cluster_arn") or ""),
        service_arn=str(corr.get("service_arn") or incident.get("service_arn") or placement.get("service_arn") or ""),
        task_arn=str(task.get("task_arn") or ""),
        container=str(placement.get("container") or (task.get("containers") or [""])[0] or ""),
        user_name=user_name,
        user_arn=principal if user_name else "",
        access_key_id=str(key_alert.get("access_key_id") or ""),
        environment=str(incident.get("environment") or ""),
    )


def respond_to_incident(
    *,
    table: Any,
    pk: str,
    incident_id: str,
    region: str,
    account_id: str = "",
    session_factory: Callable[[], Any],
    facts: Optional[Dict[str, Any]] = None,
    invoke: Optional[Callable[[str, str], Any]] = None,
    notify: Optional[Callable[[str, str], Any]] = None,
    calibration_cfg: Optional[Dict[str, int]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Choose, gate, run. Returns what happened per action.

    ``session_factory`` builds the *respond* session lazily, so an incident
    with nothing to run never assumes the responder role. ``notify(text, kind)``
    posts a channel update (kind: "chosen", "contained", "pending", "none").
    """
    incident = incidents.get_incident(table, pk, incident_id)
    if not incident:
        raise KeyError(f"incident {incident_id} not found")
    if incident.get("action_started_at") and not force:
        # A job retry (or a second call) must never run the same action twice;
        # the first run's audit rows are the record of what happened.
        logger.info("incident %s already has a response; not re-running", incident_id)
        return {"incident_id": incident_id, "action": incident.get("action"), "audit_ids": incident.get("audit_ids") or [], "skipped": "already responded"}
    alerts = incidents.alerts_for_incident(table, pk, incident_id)
    rows = load_action_rows(table, pk)
    ctx = context_from_incident(incident, alerts, region=region, account_id=account_id)
    plan = choose_actions(facts or {"incident_id": incident_id}, rows, ctx, invoke=invoke)
    by_id = {r["action_id"]: r for r in rows}

    decisions: List[Dict[str, Any]] = []
    audit_ids: List[str] = list(incident.get("audit_ids") or [])
    pending: List[Dict[str, Any]] = []
    first_started: Optional[str] = None
    verified_at: Optional[str] = None
    chosen_at = _now()

    for action_id in plan["action_ids"]:
        row = by_id[action_id]
        action = get_action(row["action_type"])
        risk = score_action(action, ctx, {}, calibration_cfg=calibration_cfg)
        if not grants.is_armed(row):
            # the grant is checked before the gate: a merged pull request arms an action, nothing else does
            decisions.append({"action_id": action_id, "action_type": row["action_type"], "risk": risk["risk"], "risk_max": row.get("risk_max"), "mode": row.get("mode"),
                              "decision": "wait_for_grant", "reason": f"grant is {row.get('grant_status') or 'not granted'}" + (f" (waiting on {row['grant_pr']})" if row.get("grant_pr") else "")})
            continue
        decision = gate(row.get("mode", "approve"), risk["risk"], int(row.get("risk_max", 2)))
        entry = {"action_id": action_id, "action_type": row["action_type"], "risk": risk["risk"], "risk_max": row.get("risk_max"), "mode": row.get("mode"), **decision}
        if decision["decision"] != "run":
            if decision["decision"] == "wait_for_approval":
                pending.append({"action_id": action_id, "action_type": row["action_type"], "risk": risk["risk"], "reason": decision["reason"]})
            decisions.append(entry)
            continue
        session = _session_for(session_factory, row)   # one session per action, narrowed to that action's grant
        if session is None:
            entry.update(decision="skipped", reason="no act role (aws.roles.act): run `compass connect`")
            decisions.append(entry)
            continue
        if first_started is None:
            first_started = _now()
            # Claim the response before the first AWS call, so a crash or retry
            # after this point sees it and does not act again.
            incidents.mark(table, pk, incident_id, action_started_at=first_started, action_chosen_at=chosen_at)
        audit = run_response_action(session=session, row=row, ctx=ctx, audit_log=AuditLog(table, pk), chosen_by=plan["chosen_by"], risk=risk)
        audit_ids.append(audit["audit_id"])
        entry.update(audit_id=audit["audit_id"], status=audit["status"])
        if audit["status"] == "verified" and (audit.get("verify") or {}).get("ok"):
            verified_at = (audit.get("verify") or {}).get("verified_at") or _now()
            entry["verify"] = {k: v for k, v in (audit.get("verify") or {}).items() if k in ("new_task", "steady_after_s", "status")}
        decisions.append(entry)

    fields: Dict[str, Any] = {
        "action": {"plan": plan, "decisions": decisions, "pending": pending, "context": {"task_arn": ctx.task_arn, "service_arn": ctx.service_arn, "user_arn": ctx.user_arn}},
        "audit_ids": audit_ids,
        "action_chosen_at": chosen_at,
    }
    if first_started:
        fields["action_started_at"] = first_started
    if verified_at:
        fields["action_verified_at"] = verified_at
    incidents.mark(table, pk, incident_id, **fields)

    if notify:
        try:
            if verified_at:
                ran = [d for d in decisions if d.get("status") == "verified"]
                notify(f"Contained: {', '.join(d['action_type'] for d in ran)} verified at {verified_at}.", "contained")
            elif pending:
                notify(f"Waiting for approval: {', '.join(p['action_type'] for p in pending)} (risk {pending[0]['risk']}).", "pending")
            elif plan["action_ids"]:
                notify("Action attempted but not verified; see the audit.", "chosen")
            else:
                notify("No enabled action covers this target; recommendation only.", "none")
        except Exception as exc:  # noqa: BLE001
            logger.warning("incident update failed: %s", exc)
    return {"incident_id": incident_id, **fields}


def approve_pending_action(
    *,
    table: Any,
    pk: str,
    incident_id: str,
    action_id: str,
    region: str,
    account_id: str = "",
    session_factory: Callable[[], Any],
    approved_by: str,
) -> Dict[str, Any]:
    """Run one parked action with a human's approval (mode ``approve``)."""
    incident = incidents.get_incident(table, pk, incident_id)
    if not incident:
        raise KeyError(f"incident {incident_id} not found")
    pending = [p for p in ((incident.get("action") or {}).get("pending") or []) if p.get("action_id") == action_id]
    if not pending:
        raise ValueError(f"action {action_id} is not pending approval on this incident")
    row = table.get_item(Key={"pk": pk, "sk": honeypot_action_sort_key(action_id)}).get("Item")
    if not row:
        raise ValueError("the whitelist row no longer exists")
    if not grants.is_armed(row):
        raise RuntimeError(f"{row.get('action_type')} is {row.get('grant_status') or 'not granted'}, not armed: merge its pull request first"
                           + (f" ({row['grant_pr']})" if row.get("grant_pr") else ""))
    alerts = incidents.alerts_for_incident(table, pk, incident_id)
    ctx = context_from_incident(incident, alerts, region=region, account_id=account_id)
    session = _session_for(session_factory, row)
    if session is None:
        raise RuntimeError("no act role (aws.roles.act): run `compass connect`")
    action = get_action(row["action_type"])
    risk = score_action(action, ctx, {})
    audit = run_response_action(session=session, row=row, ctx=ctx, audit_log=AuditLog(table, pk), chosen_by="agent", approved_by=approved_by, risk=risk)
    act = dict(incident.get("action") or {})
    act["pending"] = [p for p in act.get("pending") or [] if p.get("action_id") != action_id]
    act.setdefault("decisions", []).append({"action_id": action_id, "action_type": row["action_type"], "decision": "run", "reason": f"approved by {approved_by}", "audit_id": audit["audit_id"], "status": audit["status"], "risk": risk["risk"]})
    fields: Dict[str, Any] = {"action": act, "audit_ids": list(incident.get("audit_ids") or []) + [audit["audit_id"]]}
    if not incident.get("action_started_at"):
        fields["action_started_at"] = audit["started_at"]
    if audit["status"] == "verified":
        fields["action_verified_at"] = (audit.get("verify") or {}).get("verified_at") or _now()
    incidents.mark(table, pk, incident_id, **fields)
    return {"incident_id": incident_id, "audit": audit, **fields}


def _session_for(session_factory: Callable[..., Any], row: Dict[str, Any]) -> Any:
    """The factory takes the row (the session is narrowed to that grant); an older factory
    that takes nothing is still called."""
    try:
        return session_factory(row)
    except TypeError:
        return session_factory()


def responder_session_factory(config: Dict[str, Any], region: str, *, approver: Optional[str] = None) -> Callable[..., Any]:
    """An act session for one armed grant: the act role, narrowed to that action's statements,
    attributed to the person whose approval runs it. Never cached: one session per action."""
    from compass_core import sessions

    def _make(row: Optional[Dict[str, Any]] = None):
        try:
            action = grants.narrowing(row, config) if row is not None else None
            return sessions.target("act", region=region, config=config, approver=approver, action=action)
        except grants.GrantError as exc:
            logger.warning("%s", exc)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("cannot build responder session: %s", exc)
            return None

    return _make


def agent_invoke_from_env() -> Optional[Callable[[str, str], Any]]:
    if os.getenv("HONEYPOT_AGENT_CHOICE", "").lower() != "llm":
        return None
    from captains.honeypot.summary import default_invoke

    return default_invoke
