"""Demo mode's reset, this captain's part (docs/demo.md): close every open incident as
``demo reset``, roll back every key deactivation still in force, arm the pre-armed decoys
again and wait for the replay service's steady state. ``compass demo reset`` runs it first,
as the command ``demo_reset``; what `demo/replay/run.sh reset` did by hand, in the core."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from boto3.dynamodb.conditions import Key

from captains.honeypot import commands as honeypot_commands
from captains.honeypot import demo_rearm, incidents
from captains.honeypot.actions.base import cluster_arn_of_service
from captains.honeypot.placement import to_native
from compass_core import demo, sessions
from compass_core.keys import honeypot_audit_prefix, honeytoken_sort_key, workspace_pk
from compass_core.tables import dynamo_table, tables
from compass_core.workspace import captain_config, resolve

logger = logging.getLogger("honeypot.demo_reset")

STEADY_STATE_TIMEOUT_S = 300.0


def demo_reset(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Demo mode only. Incidents closed, keys rolled back, the trippable decoys armed, the replay
    service at steady state; each step counted, a failure recorded and the rest still done."""
    demo.require_demo_mode("honeypot demo_reset")
    workspace = resolve(payload)
    config = captain_config(payload, workspace)
    table = dynamo_table(tables(config).items)
    pk = workspace_pk(workspace.workspace_id)
    trippable = [str(t) for t in demo.settings(config)["trippable"]]
    out: Dict[str, Any] = {"incidents_closed": 0, "rolled_back": 0, "rearmed": [], "failed": [], "steady_state": None}

    for incident in incidents.list_incidents(table, pk, limit=500):
        if not incident.get("closed_at"):
            incidents.mark(table, pk, str(incident["incident_id"]), closed_at=incidents.now_iso(), resolution="demo reset")
            out["incidents_closed"] += 1

    with sessions.captain_context("honeypot", requester=sessions.SCHEDULER):
        for audit in _audits(table, pk):
            if audit.get("action_type") == "iam_deactivate_key" and audit.get("status") == "verified" and not (audit.get("rollback") or {}).get("done_at"):
                try:
                    rolled = demo_rearm._rollback(table, pk, audit, config)
                    if (rolled.get("rollback") or {}).get("error"):
                        raise RuntimeError(rolled["rollback"]["error"])
                    out["rolled_back"] += 1
                except Exception as exc:  # noqa: BLE001 - the next step still runs; the summary says
                    out["failed"].append({"audit_id": audit.get("audit_id"), "error": f"{type(exc).__name__}: {exc}"[:300]})
        for honeytoken_id in trippable:
            try:
                armed = honeypot_commands.arm({"workspace_id": workspace.workspace_id, "honeytoken_id": honeytoken_id})
                if armed.get("status") != "armed":
                    failed = [c for c in armed.get("checks") or [] if not c.get("ok")]
                    raise RuntimeError("not ready to arm: " + "; ".join(f"{c.get('name')}: {c.get('detail')}" for c in failed))
                out["rearmed"].append(honeytoken_id)
            except Exception as exc:  # noqa: BLE001
                out["failed"].append({"honeytoken_id": honeytoken_id, "error": f"{type(exc).__name__}: {exc}"[:300]})
        out["steady_state"] = wait_for_steady_state(table, pk, trippable, config, timeout_s=float(payload.get("steady_state_timeout") or STEADY_STATE_TIMEOUT_S))
    return out


def _audits(table: Any, pk: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(honeypot_audit_prefix())}
    while True:
        page = table.query(**kwargs)
        rows.extend(to_native({k: v for k, v in item.items() if k not in ("pk", "sk")}) for item in page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return rows
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def wait_for_steady_state(table: Any, pk: str, trippable: List[str], config: Dict[str, Any], *, timeout_s: float = STEADY_STATE_TIMEOUT_S,
                          sleep: Any = time.sleep, clock: Any = time.monotonic) -> Optional[Dict[str, Any]]:
    """The ECS service the pre-armed decoys sit in (their placement), until it runs as many
    tasks as it wants. None when no decoy names a service or the account cannot be read."""
    services: Dict[str, str] = {}
    for honeytoken_id in trippable:
        item = table.get_item(Key={"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)}).get("Item") or {}
        placement = to_native(item.get("placement") or {})
        if placement.get("service_arn"):
            services[str(placement["service_arn"])] = str(placement.get("cluster_arn") or cluster_arn_of_service(str(placement["service_arn"])))
    if not services:
        return None
    out: Dict[str, Any] = {}
    deadline = clock() + timeout_s
    for service_arn, cluster_arn in services.items():
        region = service_arn.split(":")[3] if service_arn.count(":") >= 5 else None
        try:
            session = sessions.target("read", region=region, captain="honeypot", config=config)
            if session is None:
                return None
            ecs = session.client("ecs")
            while True:
                described = ecs.describe_services(cluster=cluster_arn, services=[service_arn]).get("services") or []
                if not described:
                    out[service_arn] = {"steady": False, "note": "no such service"}
                    break
                running, desired = int(described[0].get("runningCount") or 0), int(described[0].get("desiredCount") or 0)
                if running == desired:
                    out[service_arn] = {"steady": True, "running": running, "desired": desired}
                    break
                if clock() >= deadline:
                    out[service_arn] = {"steady": False, "running": running, "desired": desired, "note": f"not at steady state within {int(timeout_s)} s"}
                    break
                sleep(5)
        except Exception as exc:  # noqa: BLE001 - the reset goes on; the summary says
            out[service_arn] = {"steady": False, "note": f"{type(exc).__name__}: {exc}"[:200]}
    return out
