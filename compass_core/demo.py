"""Demo mode: a synthetic world beside a real account, for a Compass strangers try.

``APP_MODE=demo`` turns demo mode on for the API, the dashboard and the daemon alike;
the ``demo`` block of the workspace config says what the demo is (docs/demo.md).
Outside demo mode the ``demo`` commands refuse in one line and the demo routes
answer 404.

``compass demo seed [resources=…] [seed=…]`` writes the synthetic world
(``infra_map.synthetic``) to the map as one scan under the scope ``demo:seed:<n>``,
next to whatever the mapper scanned. The mapper's scans never close it (they list
their own scopes), a second seed with the same numbers changes nothing, and a seed
with other numbers closes the old world and writes the new one.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from compass_core.aws import resolve_target_account_id
from compass_core.events import current_job_id
from compass_core.keys import demo_cooldown_prefix, demo_cooldown_sort_key, demo_visitor_sort_key
from compass_core.workspace import resolve
from infra_map import synthetic
from infra_map.graph_store import open_store
from infra_map.migrate import workspace_org

logger = logging.getLogger("compass.demo")

REPO_OWNER = "acme-demo"

DEFAULTS: Dict[str, Any] = {
    "world": {"resources": 10_000, "seed": 7},
    "trippable": [],
    "cooldown_seconds": 300,
    "trips_per_visitor_per_hour": 3,
    "visitor_repo": "",
    "seeded_pr": {},
    "reset": {"schedule": "cron(0 3 ? * SUN *)", "prune_jobs_days": 7},
    "pinned_settings": ["github", "aws", "demo", "notifications.endpoints"],
    "links": {"install": "https://beamreach.ai/compass/install.html", "repository": "https://github.com/beamreach-ai/compass"},
}


def enabled() -> bool:
    """Demo mode is the process's ``APP_MODE``: the same line the API and the dashboard read."""
    return (os.getenv("APP_MODE") or "").strip().lower() == "demo"


def settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """The ``demo`` block over the defaults; one level deep, which is as deep as the block goes."""
    block = config.get("demo") if isinstance(config.get("demo"), dict) else {}
    out: Dict[str, Any] = {}
    for key, default in DEFAULTS.items():
        value = block.get(key)
        if isinstance(default, dict):
            out[key] = {**default, **(value if isinstance(value, dict) else {})}
        else:
            out[key] = value if value is not None else default
    return out


def require_demo_mode(what: str) -> None:
    if not enabled():
        raise ValueError(f"{what} runs only in demo mode: set APP_MODE=demo on the services (docs/demo.md)")


def seed(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Write the synthetic world to the map under ``demo:seed:<seed>``, as one scan."""
    workspace = resolve(payload)
    config = dict(workspace.config)
    config.setdefault("workspace_id", workspace.workspace_id)
    require_demo_mode("demo seed")
    world_settings = settings(config)["world"]
    resources = int(payload.get("resources") or world_settings["resources"])
    seed_number = int(payload.get("seed") if payload.get("seed") is not None else world_settings["seed"])
    if resources < 1:
        raise ValueError("resources must be at least 1")
    account = resolve_target_account_id(config)
    if not account:
        raise ValueError("the target account is not known: run `compass connect` first, or set aws.account_id in the workspace config")

    org_id = workspace_org(config, workspace.workspace_id)
    scope = synthetic.world_scope(seed_number)
    # the repositories are `acme-demo/...`: a row shares an id with a real one only by its id, and a real
    # repository called acme/infra (the e2e world has one) must not be rewritten as synthetic
    world = synthetic.generate(synthetic.WorldSpec(resources=resources, seed=seed_number, org_id=org_id, account=account, scope=scope, synthetic=True,
                                                   repo_owner=REPO_OWNER))
    store = open_store(config, workspace_id=workspace.workspace_id)
    try:
        # the scan covers the new world's pairs and every synthetic pair already on the map, so a
        # world seeded with other numbers is closed by this one; the real scans' pairs stay out of it
        before = store.scopes(org_id)
        old = sorted(pair for pair in before if synthetic.is_synthetic_scope(pair[1]))
        pairs = sorted({(n.source or "", scope) for n in world.nodes} | {(e.source or "", scope) for e in world.edges} | set(old))
        with store.begin_scan(org_id, scopes=pairs, job_id=current_job_id() or "demo:seed") as scan:
            scan.put_nodes(world.nodes)
            scan.put_edges(world.edges)
        after = store.scopes(org_id)
    finally:
        store.close()
    done = scan.scan
    logger.info("seeded the synthetic world %s: %d nodes, %d edges (added %d, changed %d, removed %d)",
                scope, world.node_count, world.edge_count, done.added, done.changed, done.removed)
    return {
        "workspace_id": workspace.workspace_id, "org_id": org_id, "scope": scope, "resources": resources, "seed": seed_number,
        "nodes": world.node_count, "edges": world.edge_count,
        "scan": {"status": done.status, "added": done.added, "changed": done.changed, "removed": done.removed, "job_id": done.job_id},
        "closed_scopes": [{"source": s, "scope": c} for s, c in old if c != scope],
        "map": by_scope(after),
    }


def by_scope(counts: Dict[Tuple[str, str], int]) -> List[Dict[str, Any]]:
    """The map's rows per (source, scope), synthetic pairs marked, biggest first."""
    rows = [{"source": source, "scope": scope, "rows": count, "synthetic": synthetic.is_synthetic_scope(scope)} for (source, scope), count in counts.items()]
    return sorted(rows, key=lambda r: (-int(r["rows"]), str(r["source"]), str(r["scope"])))


def demo(payload: Dict[str, Any]) -> Dict[str, Any]:
    """seed [resources=10000] [seed=7]: the synthetic world on the map (demo mode only)."""
    verb = str(payload.get("verb") or "").strip().lower()
    if verb == "seed":
        return seed(payload)
    raise ValueError("verb must be seed: `compass demo seed [resources=10000] [seed=7]`")


# ── Trip it: one real use of a pre-armed decoy, paced ────────────────────────
#
# A trip is a row DEMO#COOLDOWN#<token>: who tripped, when, until when the decoy is
# "taken", and the alert and incident the trip produced once they exist. Claiming it is a
# conditional write, so two visitors pressing at once get one trip and one countdown.

def epoch_ms() -> int:
    return int(time.time() * 1000)


def cooldown_row(table: Any, pk: str, honeytoken_id: str) -> Optional[Dict[str, Any]]:
    item = table.get_item(Key={"pk": pk, "sk": demo_cooldown_sort_key(honeytoken_id)}).get("Item")
    return _native(item) if item else None


def in_force(row: Optional[Dict[str, Any]], *, now_ms: int) -> bool:
    return bool(row) and int(row.get("until") or 0) > now_ms


def claim_trip(table: Any, pk: str, honeytoken_id: str, *, user_id: str, cooldown_seconds: int, now_ms: int) -> Dict[str, Any]:
    """Take the decoy for ``cooldown_seconds``: ``{"claimed": True, "row": …}``, or the trip
    already in force when another visitor was first."""
    from boto3.dynamodb.conditions import Attr
    from botocore.exceptions import ClientError

    row = {"pk": pk, "sk": demo_cooldown_sort_key(honeytoken_id), "honeytoken_id": honeytoken_id, "user_id": user_id,
           "tripped_at": now_ms, "until": now_ms + int(cooldown_seconds) * 1000, "cooldown_seconds": int(cooldown_seconds)}
    try:
        table.put_item(Item=row, ConditionExpression=Attr("pk").not_exists() | Attr("until").lt(now_ms))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        return {"claimed": False, "row": cooldown_row(table, pk, honeytoken_id)}
    return {"claimed": True, "row": row}


def record_trip(table: Any, pk: str, honeytoken_id: str, **fields: Any) -> None:
    """What the trip produced, as it happens: the job, the fire, the alert, the incident."""
    names = {f"#f{i}": k for i, k in enumerate(fields)}
    values = {f":v{i}": v for i, v in enumerate(fields.values())}
    table.update_item(Key={"pk": pk, "sk": demo_cooldown_sort_key(honeytoken_id)},
                      UpdateExpression="SET " + ", ".join(f"{n} = :v{i}" for i, n in enumerate(names)),
                      ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def cooldowns(table: Any, pk: str, *, now_ms: int) -> List[Dict[str, Any]]:
    """Every trippable decoy's last trip, in force or not."""
    from boto3.dynamodb.conditions import Key

    rows = table.query(KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(demo_cooldown_prefix())).get("Items", [])
    return [{**_native(r), "in_force": in_force(_native(r), now_ms=now_ms)} for r in rows]


def public_cooldown(row: Dict[str, Any], *, me: Optional[str], now_ms: int) -> Dict[str, Any]:
    """A trip as a visitor may see it: no identity, only whether it was theirs."""
    return {
        "honeytoken_id": row.get("honeytoken_id"), "tripped_at": row.get("tripped_at"), "until": row.get("until"),
        "in_force": in_force(row, now_ms=now_ms), "seconds_left": max(0, (int(row.get("until") or 0) - now_ms) // 1000),
        "by": "you" if me and row.get("user_id") == me else "a visitor",
        "job_id": row.get("job_id"), "fired_at": row.get("fired_at"), "alert_id": row.get("alert_id"),
        "alert_received_at": row.get("alert_received_at"), "incident_id": row.get("incident_id"),
        "rearmed_at": row.get("rearmed_at"), "rearm_error": row.get("rearm_error"),
    }


def visitor_trips(table: Any, pk: str, user_id: str, *, now_ms: int, window_ms: int = 3_600_000) -> List[int]:
    """When this visitor tripped in the last hour."""
    item = table.get_item(Key={"pk": pk, "sk": demo_visitor_sort_key(user_id)}).get("Item") or {}
    return sorted(int(t) for t in (item.get("trips") or []) if int(t) > now_ms - window_ms)


def record_visitor_trip(table: Any, pk: str, user_id: str, *, now_ms: int) -> None:
    trips = visitor_trips(table, pk, user_id, now_ms=now_ms) + [now_ms]
    table.put_item(Item={"pk": pk, "sk": demo_visitor_sort_key(user_id), "user_id": user_id, "trips": trips, "updated_at": now_ms})


def _native(item: Any) -> Any:
    from decimal import Decimal

    if isinstance(item, dict):
        return {k: _native(v) for k, v in item.items()}
    if isinstance(item, list):
        return [_native(v) for v in item]
    if isinstance(item, Decimal):
        return int(item) if item == item.to_integral_value() else float(item)
    return item


# ── demo-rearm: after the response verified, undo the key deactivation and arm again ──

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
    now_ms = now_ms or epoch_ms()
    out: Dict[str, Any] = {"rearmed": [], "waiting": [], "failed": []}
    for row in cooldowns(table, pk, now_ms=now_ms):
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
                    audit = _native({k: v for k, v in (item or {}).items() if k not in ("pk", "sk")})
                    if audit.get("action_type") == "iam_deactivate_key" and audit.get("status") == "verified" and not (audit.get("rollback") or {}).get("done_at"):
                        rolled = _rollback(table, pk, audit, config)
                        if (rolled.get("rollback") or {}).get("error"):
                            raise RuntimeError(f"rollback of {audit_id} failed: {rolled['rollback']['error']}")
                armed = honeypot_commands.arm({"workspace_id": workspace_id, "honeytoken_id": token_id})
            if armed.get("status") != "armed":
                failed = [c for c in armed.get("checks") or [] if not c.get("ok")]
                raise RuntimeError("not ready to arm: " + "; ".join(f"{c.get('name')}: {c.get('detail')}" for c in failed))
            record_trip(table, pk, token_id, rearmed_at=epoch_ms(), rearm_error=None)
            out["rearmed"].append(token_id)
            logger.info("demo: decoy %s re-armed after incident %s", token_id, row["incident_id"])
        except Exception as exc:  # noqa: BLE001 - the row says why; the next tick tries again
            record_trip(table, pk, token_id, rearm_error=f"{type(exc).__name__}: {exc}"[:300])
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
    if not enabled():
        return
    wait = float(interval or 30)
    while True:
        try:
            rearm_once(config, workspace_id=workspace_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("demo re-arm tick failed: %s", exc)
        if stop.wait(wait):
            return
