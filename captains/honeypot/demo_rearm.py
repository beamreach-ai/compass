"""Demo mode's re-arm (docs/demo.md): after a tripped decoy's response verified, roll the key
deactivation back and arm the decoy again, so the next visitor can trip it. A `compass serve`
service of this captain (`demo-rearm`, every 30 s), active in demo mode only."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from compass_core import demo

logger = logging.getLogger("honeypot.demo_rearm")


def rearm_once(config: Dict[str, Any], *, workspace_id: str, table: Any = None, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Every tripped decoy whose incident has ``action_verified_at``: roll back the
    ``iam_deactivate_key`` audits not yet rolled back (the existing rollback, through the
    act role under the grant), then run ``arm`` for the token. The incident stays open for
    other visitors to read; the cooldown row keeps the incident id and takes ``rearmed_at``."""
    from captains.honeypot import commands as honeypot_commands
    from captains.honeypot.incidents import get_incident
    from compass_core import sessions
    from compass_core.keys import honeypot_audit_sort_key, workspace_pk
    from compass_core.tables import dynamo_table, tables

    table = table or dynamo_table(tables(config).items)
    pk = workspace_pk(workspace_id)
    now_ms = now_ms or demo.epoch_ms()
    out: Dict[str, Any] = {"rearmed": [], "waiting": [], "failed": []}
    for row in demo.cooldowns(table, pk, now_ms=now_ms):
        token_id = str(row.get("honeytoken_id") or "")
        if not row.get("incident_id") or row.get("rearmed_at"):
            continue
        incident = get_incident(table, pk, str(row["incident_id"]))
        if not incident or not incident.get("action_verified_at"):
            out["waiting"].append(token_id)
            continue
        try:
            with sessions.captain_context("honeypot", requester=sessions.SCHEDULER):
                for audit_id in incident.get("audit_ids") or []:
                    item = table.get_item(Key={"pk": pk, "sk": honeypot_audit_sort_key(str(audit_id))}).get("Item")
                    audit = demo._native({k: v for k, v in (item or {}).items() if k not in ("pk", "sk")})
                    if audit.get("action_type") == "iam_deactivate_key" and audit.get("status") == "verified" and not (audit.get("rollback") or {}).get("done_at"):
                        rolled = _rollback(table, pk, audit, config)
                        if (rolled.get("rollback") or {}).get("error"):
                            raise RuntimeError(f"rollback of {audit_id} failed: {rolled['rollback']['error']}")
                armed = honeypot_commands.arm({"workspace_id": workspace_id, "honeytoken_id": token_id})
            if armed.get("status") != "armed":
                failed = [c for c in armed.get("checks") or [] if not c.get("ok")]
                raise RuntimeError("not ready to arm: " + "; ".join(f"{c.get('name')}: {c.get('detail')}" for c in failed))
            demo.record_trip(table, pk, token_id, rearmed_at=demo.epoch_ms(), rearm_error=None)
            out["rearmed"].append(token_id)
            logger.info("demo: decoy %s re-armed after incident %s", token_id, row["incident_id"])
        except Exception as exc:  # noqa: BLE001 - the row says why; the next tick tries again
            demo.record_trip(table, pk, token_id, rearm_error=f"{type(exc).__name__}: {exc}"[:300])
            out["failed"].append({"honeytoken_id": token_id, "error": str(exc)[:300]})
            logger.warning("demo: re-arming %s failed: %s", token_id, exc)
    return out


def _rollback(table: Any, pk: str, audit: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """The rollback route's work (compass_api.routes.honeypot_actions) without a request."""
    from captains.honeypot.actions import get_action
    from captains.honeypot.actions.base import ActionContext
    from captains.honeypot.responder import AuditLog, rollback_response_action
    from compass_core import grants, sessions
    from compass_core.aws import resolve_target_region
    from compass_core.keys import honeypot_action_sort_key

    action = get_action(str(audit.get("action_type") or ""))
    target = audit.get("target") or {}
    user_arn = str(target.get("user_arn") or "")
    ctx = ActionContext(
        region=str(target.get("region") or resolve_target_region(config) or "us-east-1"), incident_id=str(audit.get("incident_id") or ""),
        cluster_arn=str(target.get("cluster_arn") or ""), service_arn=str(target.get("service_arn") or ""), task_arn=str(target.get("task_arn") or ""),
        container=str(target.get("container") or ""), user_arn=user_arn, user_name=user_arn.rsplit("/", 1)[-1] if user_arn else "",
        access_key_id=str(target.get("access_key_id") or ""),
    )
    grant_row = table.get_item(Key={"pk": pk, "sk": honeypot_action_sort_key(str(audit.get("action_id") or ""))}).get("Item") if audit.get("action_id") else None
    narrowing = grants.narrowing(dict(grant_row), config) if grant_row else None
    session = sessions.target("act", region=ctx.region, captain="honeypot", config=config, action=narrowing)
    if session is None:
        raise RuntimeError("no act role (aws.roles.act) for this workspace")
    return rollback_response_action(session=session, audit=audit, ctx=ctx, audit_log=AuditLog(table, pk), requested_by="demo-rearm", action=action)


def run_rearm_service(workspace_id: str, config: Dict[str, Any], stop: Any, *, interval: Optional[int] = None) -> None:
    """A `compass serve` service (ServiceSpec `demo-rearm`), active in demo mode only."""
    if not demo.enabled():
        return
    wait = float(interval or 30)
    while True:
        try:
            rearm_once(config, workspace_id=workspace_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("demo re-arm tick failed: %s", exc)
        if stop.wait(wait):
            return
