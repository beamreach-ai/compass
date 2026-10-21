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
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    "world": {"resources": 10_000, "seed": 7, "edges": 20_000},  # edges: at least this many; the rules alone give about 2.6 per resource
    "trippable": [],
    "cooldown_seconds": 300,
    "trips_per_visitor_per_hour": 3,
    "visitor_repo": "",
    "seeded_pr": {},
    "reset": {"schedule": "cron(0 3 ? * SUN *)", "prune_jobs_days": 7},
    "tour": {"import_resource": "acme-legacy-exports"},  # the real, unmanaged resource step 3 imports
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
    edges = int(payload.get("edges") if payload.get("edges") is not None else world_settings.get("edges") or 0)
    if resources < 1:
        raise ValueError("resources must be at least 1")
    if edges < 0:
        raise ValueError("edges must be 0 (the rules alone) or the number of edges wanted")
    account = resolve_target_account_id(config)
    if not account:
        raise ValueError("the target account is not known: run `compass connect` first, or set aws.account_id in the workspace config")

    org_id = workspace_org(config, workspace.workspace_id)
    scope = synthetic.world_scope(seed_number)
    # the repositories are `acme-demo/...`: a row shares an id with a real one only by its id, and a real
    # repository called acme/infra (the e2e world has one) must not be rewritten as synthetic
    world = synthetic.generate(synthetic.WorldSpec(resources=resources, seed=seed_number, edges=edges, org_id=org_id, account=account, scope=scope,
                                                   synthetic=True, repo_owner=REPO_OWNER))
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
    seeded: Dict[str, Any] = {}
    for name, seeder in _seeders.items():
        seeded[name] = seeder(config, workspace.workspace_id, world)
        logger.info("seeded %s: %s", name, seeded[name])
    return {
        "workspace_id": workspace.workspace_id, "org_id": org_id, "scope": scope, "resources": resources, "seed": seed_number,
        "nodes": world.node_count, "edges": world.edge_count,
        "scan": {"status": done.status, "added": done.added, "changed": done.changed, "removed": done.removed, "job_id": done.job_id},
        "closed_scopes": [{"source": s, "scope": c} for s, c in old if c != scope],
        "map": by_scope(after),
        "seeded": seeded,
    }


# Whatever else a deployment derives from the world (the product seeds its FinOps findings from it)
# is written after the map, by a seeder registered here: ``fn(config, workspace_id, world) -> result``,
# idempotent, since the reset seeds again. The open edition registers none.
Seeder = Callable[[Dict[str, Any], str, synthetic.World], Any]
_seeders: Dict[str, Seeder] = {}


def register_seeder(name: str, seeder: Seeder) -> None:
    _seeders[name] = seeder


def seeders() -> Dict[str, Seeder]:
    return dict(_seeders)


def by_scope(counts: Dict[Tuple[str, str], int]) -> List[Dict[str, Any]]:
    """The map's rows per (source, scope), synthetic pairs marked, biggest first."""
    rows = [{"source": source, "scope": scope, "rows": count, "synthetic": synthetic.is_synthetic_scope(scope)} for (source, scope), count in counts.items()]
    return sorted(rows, key=lambda r: (-int(r["rows"]), str(r["source"]), str(r["scope"])))


def demo(payload: Dict[str, Any]) -> Dict[str, Any]:
    """seed [resources=10000] [seed=7]: the synthetic world on the map (demo mode only)."""
    verb = str(payload.get("verb") or "").strip().lower()
    if verb == "seed":
        return seed(payload)
    raise ValueError("verb must be seed here: `compass demo seed [resources=10000] [seed=7]`; reset and status are compass_core.demo_reset")


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


# ── The tour: seven steps, each completed by a fact about the world ──────────
#
# State is one row per visitor, DEMO#TOUR#<user>: the client-only steps the dashboard
# reported and the skips. The other steps are read from the world on every GET: a pull
# request by this visitor, their trip, the response on its incident.

TOUR_STEPS: List[Dict[str, Any]] = [
    {"id": "map", "kind": "client", "title": "See the account",
     "why": "10,000 resources across four regions, every one with what it can reach: search `payments`, open a role, follow the edges.",
     "done_when": "You opened a node's detail."},
    {"id": "unmanaged", "kind": "client", "title": "What Terraform doesn't manage",
     "why": "40% of this account is not in any state file; the IaC tab lists it, with the drift on what is.",
     "done_when": "You looked at the IaC tab."},
    {"id": "import_pr", "kind": "api", "title": "Open an import pull request",
     "why": "Import a real, unmanaged resource: Compass writes the Terraform and opens the pull request; nothing is applied in the demo.",
     "done_when": "A pull request by you exists."},
    {"id": "decoys", "kind": "client", "title": "Meet the decoys",
     "why": "Two armed decoys sit next to the replay service's real secret, where an intruder would look first.",
     "done_when": "You opened a decoy's detail."},
    {"id": "trip", "kind": "api", "title": "Trip one",
     "why": "Trip it uses the decoy's key for real; CloudTrail delivers the event in one to five minutes and the incident opens.",
     "done_when": "Your trip opened an incident, or you were shown the one in force."},
    {"id": "response", "kind": "api", "title": "Watch the response",
     "why": "The investigation, the blast radius, the gate, the restart and the key going inactive, under a minute after the alert.",
     "done_when": "The response on that incident verified."},
    {"id": "run_it", "kind": "client", "title": "Run it yourself",
     "why": "The install is a compose file; `compass connect` uses your profile once and the services never see it.",
     "done_when": "You opened the install page or the repository."},
]
TOUR_STEP_IDS = [s["id"] for s in TOUR_STEPS]


def tour_row(table: Any, pk: str, user_id: str) -> Dict[str, Any]:
    from compass_core.keys import demo_tour_sort_key

    item = table.get_item(Key={"pk": pk, "sk": demo_tour_sort_key(user_id)}).get("Item")
    return _native(item) if item else {"user_id": user_id, "steps": {}}


def mark_tour_step(table: Any, pk: str, user_id: str, step: str, *, skipped: bool = False, detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Record a client-only step as done (or any step as skipped) for this visitor, once."""
    from compass_core.keys import demo_tour_sort_key

    if step not in TOUR_STEP_IDS:
        raise LookupError(f"no tour step {step!r}: one of {', '.join(TOUR_STEP_IDS)}")
    row = tour_row(table, pk, user_id)
    steps = dict(row.get("steps") or {})
    now = epoch_ms()
    entry = dict(steps.get(step) or {})
    if skipped:
        entry.setdefault("skipped_at", now)
    else:
        entry.setdefault("done_at", now)
    if detail:
        entry.update({k: v for k, v in detail.items() if v is not None})
    steps[step] = entry
    row = {"pk": pk, "sk": demo_tour_sort_key(user_id), "user_id": user_id, "steps": steps,
           "started_at": row.get("started_at") or now, "updated_at": now}
    table.put_item(Item=row)
    return _native(row)


def same_visitor(a: Optional[str], b: Optional[str]) -> bool:
    """The open edition has one login and no user id; two missing ids are the same visitor."""
    return (a or "anonymous") == (b or "anonymous")


def tour(table: Any, pk: str, config: Dict[str, Any], *, user_id: Optional[str], prs: List[Dict[str, Any]], now_ms: Optional[int] = None) -> Dict[str, Any]:
    """The visitor's tour: every step with whether it is done, from the row and from the world."""
    from captains.honeypot.incidents import get_incident

    me = user_id or "anonymous"
    now_ms = now_ms or epoch_ms()
    row = tour_row(table, pk, me)
    recorded: Dict[str, Dict[str, Any]] = dict(row.get("steps") or {})
    settings_ = settings(config)
    trippable = list(settings_["trippable"])

    # the world's facts
    my_pr = next((p for p in prs if (p.get("origin") or {}).get("command") == "import" and same_visitor((p.get("origin") or {}).get("user_id"), user_id)), None)
    trips = [r for r in cooldowns(table, pk, now_ms=now_ms) if r.get("honeytoken_id") in trippable]
    my_trip = next((r for r in sorted(trips, key=lambda r: -int(r.get("tripped_at") or 0)) if same_visitor(r.get("user_id"), user_id) and r.get("fired_at")), None)
    shown = recorded.get("trip") or {}
    # the visitor's own trip is theirs even before its alert landed: never another visitor's incident in its place
    incident_id = my_trip.get("incident_id") if my_trip else shown.get("incident_id")
    incident = get_incident(table, pk, str(incident_id)) if incident_id else None

    facts: Dict[str, Dict[str, Any]] = {
        "import_pr": {"done": bool(my_pr), "detail": {"pr_url": (my_pr or {}).get("pr_url") or (my_pr or {}).get("url")} if my_pr else {}},
        "trip": {"done": bool(my_trip) or bool(shown.get("done_at")),
                 "detail": {"incident_id": incident_id, "fired_at": (my_trip or {}).get("fired_at"), "by": "you" if my_trip else ("a visitor" if incident_id else None)}},
        "response": {"done": bool(incident and incident.get("action_verified_at")),
                     "detail": {"incident_id": incident_id, "action_verified_at": (incident or {}).get("action_verified_at")}},
    }
    steps: List[Dict[str, Any]] = []
    for spec in TOUR_STEPS:
        entry = recorded.get(spec["id"]) or {}
        fact = facts.get(spec["id"])
        done = bool(fact["done"]) if fact else bool(entry.get("done_at"))
        detail = dict((fact or {}).get("detail") or {k: v for k, v in entry.items() if k not in ("done_at", "skipped_at")})
        # what the dashboard needs to open the step: the search term, the decoy, the incident, the links
        if spec["id"] == "map":
            detail["search"] = "payments"
        elif spec["id"] == "import_pr":
            detail["search"] = settings_["tour"]["import_resource"]
        elif spec["id"] in ("decoys", "trip"):
            detail["token"] = trippable[0] if trippable else None
        elif spec["id"] == "run_it":
            detail["links"] = settings_["links"]
        steps.append({**spec, "done": done, "skipped": bool(entry.get("skipped_at")), "done_at": entry.get("done_at") if not fact else None, "detail": detail})
    completed = sum(1 for s in steps if s["done"])
    passed = sum(1 for s in steps if s["done"] or s["skipped"])
    current = next((s["id"] for s in steps if not (s["done"] or s["skipped"])), None)
    return {"steps": steps, "completed": completed, "total": len(steps), "current": current, "finished": passed == len(steps),
            "started_at": row.get("started_at"), "updated_at": row.get("updated_at")}


def tour_completions(table: Any, pk: str, *, since_ms: int) -> int:
    """Visitors whose tour reached its end since ``since_ms`` (for `demo status`)."""
    from boto3.dynamodb.conditions import Key

    from compass_core.keys import demo_tour_prefix

    rows = table.query(KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(demo_tour_prefix())).get("Items", [])
    count = 0
    for row in rows:
        steps = _native(row.get("steps") or {})
        if int(row.get("updated_at") or 0) >= since_ms and all(k in steps for k in ("map", "unmanaged", "decoys", "run_it")):
            count += 1
    return count
