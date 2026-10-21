"""Execute a whitelisted response action with the four things every Compass
write needs: a before-snapshot, a verify step, a rollback, and an audit record.

The audit row is written before the first AWS call and updated after each
step, so a crash mid-way still leaves a record saying how far it got.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from captains.honeypot.actions import get_action
from captains.honeypot.actions.base import ActionContext, ActionScope, ResponseAction, ScopeError
from compass_core import grants, sessions
from compass_core.keys import honeypot_audit_sort_key

logger = logging.getLogger("honeypot-responder")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_audit_id() -> str:
    return f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


class AuditLog:
    """Writes audit rows to the workspace table. Kept tiny so tests can pass a
    moto table or a recording stub."""

    def __init__(self, table: Any, pk: str):
        self.table = table
        self.pk = pk

    def write(self, audit: Dict[str, Any]) -> None:
        item = {"pk": self.pk, "sk": honeypot_audit_sort_key(audit["audit_id"]), **audit}
        self.table.put_item(Item=_dynamo_safe(item))


def run_response_action(
    *,
    session: Any,
    row: Dict[str, Any],
    ctx: ActionContext,
    audit_log: AuditLog,
    chosen_by: str,
    approved_by: Optional[str] = None,
    risk: Optional[Dict[str, Any]] = None,
    action: Optional[ResponseAction] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """Snapshot → execute → verify, with an audit row updated at every step.

    ``row`` is the whitelist row (``action_type``, ``scope``, ``mode``…);
    the scope is enforced here, before any AWS call, in addition to the IAM
    role in the target account.
    """
    action = action or get_action(row["action_type"])
    scope = ActionScope.from_dict(row.get("scope"))
    audit: Dict[str, Any] = {
        "audit_id": new_audit_id(),
        "incident_id": ctx.incident_id,
        "action_id": row.get("action_id"),
        "action_type": action.action_type,
        "target": _target_of(ctx),
        "chosen_by": chosen_by,
        "approved_by": approved_by,
        "risk": risk,
        "status": "running",
        "started_at": _now(),
        "snapshot": None,
        "executed": None,
        "verify": None,
        "rollback": {"supported": action.rollback_supported, "done_at": None},
        "error": None,
        "finished_at": None,
    }
    if not grants.is_armed(row):
        audit.update(status="refused", error=f"wait_for_grant: {row.get('action_type')} is {row.get('grant_status') or 'not granted'}, not armed", finished_at=_now())
        audit_log.write(audit)
        return audit
    try:
        action.check_scope(scope, ctx)
    except ScopeError as exc:
        audit.update(status="refused", error=str(exc), finished_at=_now())
        audit_log.write(audit)
        return audit
    info = sessions.session_info(session)
    if info is not None:
        audit["session"] = {"name": info.name, "role_arn": info.role_arn, "source_identity": info.source_identity, "policy_hash": info.policy_hash}
    audit_log.write(audit)

    try:
        audit["snapshot"] = action.snapshot(session, ctx)
        audit_log.write(audit)
        audit["executed"] = action.execute(session, ctx, audit["snapshot"])
        audit_log.write(audit)
        audit["verify"] = action.verify(
            session, ctx, audit["snapshot"], audit["executed"], sleep=sleep, clock=clock
        )
        audit["status"] = "verified" if audit["verify"].get("ok") else "verify_failed"
    except Exception as exc:  # noqa: BLE001 - the audit row must record any failure
        logger.exception("response action %s failed", action.action_type)
        audit["status"] = "failed"
        audit["error"] = f"{type(exc).__name__}: {exc}"
    audit["finished_at"] = _now()
    audit_log.write(audit)
    return audit


def rollback_response_action(
    *,
    session: Any,
    audit: Dict[str, Any],
    ctx: ActionContext,
    audit_log: AuditLog,
    requested_by: str,
    action: Optional[ResponseAction] = None,
) -> Dict[str, Any]:
    action = action or get_action(audit["action_type"])
    if not action.rollback_supported:
        audit["rollback"] = {"supported": False, "done_at": None, "reason": action.rollback({}, ctx, {}).get("reason")}
        audit_log.write(audit)
        return audit
    try:
        result = action.rollback(session, ctx, audit.get("snapshot") or {})
        audit["rollback"] = {**result, "requested_by": requested_by}
    except Exception as exc:  # noqa: BLE001
        audit["rollback"] = {"supported": True, "done_at": None, "error": f"{type(exc).__name__}: {exc}", "requested_by": requested_by}
    audit_log.write(audit)
    return audit


def _target_of(ctx: ActionContext) -> Dict[str, str]:
    return {
        k: v
        for k, v in {
            "region": ctx.region,
            "cluster_arn": ctx.cluster_arn,
            "service_arn": ctx.service_arn,
            "task_arn": ctx.task_arn,
            "container": ctx.container,
            "user_arn": ctx.user_arn,
            "access_key_id": ctx.access_key_id,
        }.items()
        if v
    }


def _dynamo_safe(value: Any) -> Any:
    from captains.honeypot.incidents import dynamo_safe

    return dynamo_safe(value)
