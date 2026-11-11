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
    "tour": {"import_resource": "acme-legacy-exports",  # the real, unmanaged resource the import step imports
             "pilot_note": "On your own account this would be 1 of your 10 free pilot pull requests."},  # shown on PR steps when links.pilot is set
    "pinned_settings": ["github", "aws", "demo", "notifications.endpoints"],
    "admins": [],  # the email addresses that may change the demo from the dashboard (compass_api.demo_guard)
    "links": {"install": "https://beamreach.ai/compass/install.html", "repository": "https://github.com/beamreach-ai/compass",
              "slack": "",  # an invite link to the channel the alerts go to; empty hides it
              "pilot": ""},  # the pilot application page; empty hides the pilot line and the pilot exit (docs/demo.md)
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
# reported, the skips, the story the visitor arrived on and where they came from. The other
# steps are read from the world on every GET: a pull request by this visitor, their trip,
# the response on its incident.
#
# A story is an order: the visitor who clicked an ad about drift starts at the IaC tab, the
# one who clicked about decoys starts at the decoys. Every story has every step, so
# "Tour · 3 of 7" means the same thing whichever way round. A product registers steps of its
# own (register_tour_step): a story may name them, and they drop out where nobody did.

TOUR_STEPS: List[Dict[str, Any]] = [
    {"id": "map", "kind": "client", "title": "See the account",
     "why": "10,000 resources across four regions, every one with what it can reach: search `payments`, open a role, follow the edges.",
     "done_when": "You opened a node's detail."},
    {"id": "unmanaged", "kind": "client", "title": "What Terraform doesn't manage",
     "why": "40% of this account is not in any state file; the IaC tab lists it, with the drift on what is.",
     "done_when": "You looked at the IaC tab."},
    {"id": "import_pr", "kind": "api", "pr": True, "title": "Open an import pull request",
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
DEFAULT_STORY = "default"
STORIES: Dict[str, List[str]] = {
    DEFAULT_STORY: ["map", "unmanaged", "import_pr", "decoys", "trip", "response", "run_it"],
    "drift": ["unmanaged", "import_pr", "map", "decoys", "trip", "response", "run_it"],
    "honeytoken": ["decoys", "trip", "response", "map", "unmanaged", "import_pr", "run_it"],
    "cost": ["cost_fix", "unmanaged", "import_pr", "map", "decoys", "trip", "response", "run_it"],
}
SOURCE_FIELDS = ("src", "gclid", "referrer")
SOURCE_MAX_LEN = 200

# A product's step: its spec (the same keys as TOUR_STEPS, plus ``pr: True`` for a step that
# opens a pull request) and a fact: ctx -> {"done": bool, "detail": {...}}. ctx carries table,
# pk, config, user_id, prs, now_ms. The detail may carry ``href`` (a dashboard path the step
# opens) and whatever the fact wants shown.
TourFact = Callable[[Dict[str, Any]], Dict[str, Any]]
_tour_steps: Dict[str, Tuple[Dict[str, Any], TourFact]] = {}


def register_tour_step(spec: Dict[str, Any], fact: TourFact) -> None:
    if not spec.get("id") or spec["id"] in {s["id"] for s in TOUR_STEPS}:
        raise ValueError(f"a tour step needs an id of its own, not {spec.get('id')!r}")
    _tour_steps[str(spec["id"])] = (dict(spec), fact)


def unregister_tour_step(step_id: str) -> None:
    _tour_steps.pop(step_id, None)


def tour_steps() -> List[Dict[str, Any]]:
    """The core's steps, then the product's, in registration order."""
    return [dict(s) for s in TOUR_STEPS] + [dict(spec) for spec, _ in _tour_steps.values()]


def tour_step_ids() -> List[str]:
    return [s["id"] for s in tour_steps()]


def story_name(value: Optional[str]) -> Optional[str]:
    """A known story, or None for anything else (the default order)."""
    name = str(value or "").strip().lower()
    return name if name in STORIES and name != DEFAULT_STORY else None


def story_order(story: Optional[str]) -> List[str]:
    """The step ids in the story's order: the named ones first, every other registered step after,
    in its default place; a product step nobody registered drops out."""
    known = tour_step_ids()
    named = [s for s in STORIES.get(story or DEFAULT_STORY, STORIES[DEFAULT_STORY]) if s in known]
    return named + [s for s in known if s not in named]


def source_fields(values: Dict[str, Any]) -> Dict[str, str]:
    """The ad source as the dashboard reported it, clipped: src, gclid, referrer."""
    out: Dict[str, str] = {}
    for key in SOURCE_FIELDS:
        value = str(values.get(key) or "").strip()
        if value:
            out[key] = value[:SOURCE_MAX_LEN]
    return out


def tour_row(table: Any, pk: str, user_id: str) -> Dict[str, Any]:
    from compass_core.keys import demo_tour_sort_key

    item = table.get_item(Key={"pk": pk, "sk": demo_tour_sort_key(user_id)}).get("Item")
    return _native(item) if item else {"user_id": user_id, "steps": {}}


def mark_tour_step(table: Any, pk: str, user_id: str, step: str, *, skipped: bool = False, detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Record a client-only step as done (or any step as skipped) for this visitor, once."""
    from compass_core.keys import demo_tour_sort_key

    ids = tour_step_ids()
    if step not in ids:
        raise LookupError(f"no tour step {step!r}: one of {', '.join(ids)}")
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
    row = {**_row_keep(row), "pk": pk, "sk": demo_tour_sort_key(user_id), "user_id": user_id, "steps": steps,
           "started_at": row.get("started_at") or now, "updated_at": now}
    table.put_item(Item=row)
    return _native(row)


def _row_keep(row: Dict[str, Any]) -> Dict[str, Any]:
    """What a tour row carries besides its steps: the story and the source, written once, and the events."""
    return {k: row[k] for k in ("story", "source", "landed_at", "events") if row.get(k)}


TOUR_EVENTS = ("pilot_click", "install_click", "repository_click")  # what the dashboard may report besides steps
TOUR_EVENTS_MAX = 50


def record_tour_event(table: Any, pk: str, user_id: str, event: str, *, detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Something the visitor did that is not a step: the pilot link, the install page, the repository.
    Appended to the row's events, newest last, the oldest dropped past TOUR_EVENTS_MAX."""
    from compass_core.keys import demo_tour_sort_key

    if event not in TOUR_EVENTS:
        raise LookupError(f"no tour event {event!r}: one of {', '.join(TOUR_EVENTS)}")
    row = tour_row(table, pk, user_id)
    now = epoch_ms()
    events = list(row.get("events") or [])
    events.append({"event": event, "at": now, **{k: str(v)[:SOURCE_MAX_LEN] for k, v in (detail or {}).items() if v}})
    row["events"] = events[-TOUR_EVENTS_MAX:]
    row = {**_row_keep(row), "pk": pk, "sk": demo_tour_sort_key(user_id), "user_id": user_id, "steps": dict(row.get("steps") or {}),
           "started_at": row.get("started_at") or now, "updated_at": now}
    table.put_item(Item=row)
    return _native(row)


def start_tour(table: Any, pk: str, user_id: str, *, story: Optional[str] = None, source: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The visitor arrived: remember the story they came for and where from. Both are written once;
    a second landing on another story keeps the first (the tour is already under way)."""
    from compass_core.keys import demo_tour_sort_key

    row = tour_row(table, pk, user_id)
    now = epoch_ms()
    story = story_name(story)
    fields = source_fields(source or {})
    changed = False
    if story and not row.get("story"):
        row["story"] = story
        changed = True
    if fields and not row.get("source"):
        row["source"] = fields
        changed = True
    if not row.get("landed_at"):
        row["landed_at"] = now
        changed = True
    if not changed and row.get("started_at"):
        return row
    row = {**_row_keep(row), "pk": pk, "sk": demo_tour_sort_key(user_id), "user_id": user_id, "steps": dict(row.get("steps") or {}),
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
    # the product's steps, each a fact of its own
    ctx = {"table": table, "pk": pk, "config": config, "user_id": user_id, "prs": prs, "now_ms": now_ms}
    for step_id, (_, fact_fn) in _tour_steps.items():
        try:
            fact = fact_fn(ctx) or {}
        except Exception as exc:  # noqa: BLE001 - a product step that fails is a step not done, not a tour that 500s
            logger.warning("tour step %s: %s", step_id, exc)
            fact = {}
        facts[step_id] = {"done": bool(fact.get("done")), "detail": dict(fact.get("detail") or {})}

    story = story_name(row.get("story"))
    pilot = {"url": settings_["links"]["pilot"], "note": settings_["tour"]["pilot_note"]} if settings_["links"].get("pilot") else None
    by_id = {s["id"]: s for s in tour_steps()}
    steps: List[Dict[str, Any]] = []
    for step_id in story_order(story):
        spec = by_id[step_id]
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
        elif spec["id"] in ("response", "run_it"):
            detail["links"] = settings_["links"]
        if spec.get("pr") and pilot:
            detail["pilot"] = pilot
        steps.append({**spec, "done": done, "skipped": bool(entry.get("skipped_at")), "done_at": entry.get("done_at") if not fact else None, "detail": detail})
    completed = sum(1 for s in steps if s["done"])
    passed = sum(1 for s in steps if s["done"] or s["skipped"])
    current = next((s["id"] for s in steps if not (s["done"] or s["skipped"])), None)
    return {"steps": steps, "completed": completed, "total": len(steps), "current": current, "finished": passed == len(steps),
            "story": story, "source": dict(row.get("source") or {}), "pilot": pilot,
            "started_at": row.get("started_at"), "updated_at": row.get("updated_at")}


def funnel(table: Any, pk: str, *, since_ms: int, prs: List[Dict[str, Any]], now_ms: Optional[int] = None) -> Dict[str, Any]:
    """The visitors since ``since_ms``, by where they came from and which story: how many
    started, reached each step, opened a pull request, clicked the pilot link. From the tour
    rows and the registry; nothing else is counted and nobody is named."""
    from boto3.dynamodb.conditions import Key

    from compass_core.keys import demo_tour_prefix

    now_ms = now_ms or epoch_ms()
    pr_users: Dict[str, int] = {}
    for pr in prs:
        origin = pr.get("origin") if isinstance(pr.get("origin"), dict) else {}
        if origin.get("seeded"):
            continue
        who = str(origin.get("user_id") or "anonymous")
        pr_users[who] = pr_users.get(who, 0) + 1

    def bucket() -> Dict[str, Any]:
        return {"visitors": 0, "steps": {}, "pr_opened": 0, "pilot_click": 0, "install_click": 0, "repository_click": 0, "finished": 0}

    by_source: Dict[Tuple[str, str], Dict[str, Any]] = {}
    total = bucket()
    rows = table.query(KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(demo_tour_prefix())).get("Items", [])
    all_steps = tour_step_ids()
    for raw in rows:
        row = _native(raw)
        started = int(row.get("landed_at") or row.get("started_at") or 0)
        if started < since_ms:
            continue
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        key = (str(source.get("src") or "direct"), str(story_name(row.get("story")) or DEFAULT_STORY))
        steps = row.get("steps") if isinstance(row.get("steps"), dict) else {}
        events = [e.get("event") for e in (row.get("events") or []) if isinstance(e, dict)]
        done = {step for step, entry in steps.items() if isinstance(entry, dict) and entry.get("done_at")}
        who = str(row.get("user_id") or "anonymous")
        if pr_users.get(who):
            done.update(s for s in ("import_pr",) if s in all_steps)
        for target in (by_source.setdefault(key, bucket()), total):
            target["visitors"] += 1
            for step in done:
                target["steps"][step] = target["steps"].get(step, 0) + 1
            if pr_users.get(who):
                target["pr_opened"] += 1
            for name in ("pilot_click", "install_click", "repository_click"):
                if name in events:
                    target[name] += 1
            if all(step in done for step in ("map", "unmanaged", "decoys", "run_it")):
                target["finished"] += 1
    sources = [{"src": src, "story": story, **counts} for (src, story), counts in sorted(by_source.items(), key=lambda kv: (-kv[1]["visitors"], kv[0]))]
    by_story: Dict[str, Dict[str, Any]] = {}
    for entry in sources:
        agg = by_story.setdefault(entry["story"], bucket())
        for name in ("visitors", "pr_opened", "pilot_click", "install_click", "repository_click", "finished"):
            agg[name] += entry[name]
        for step, n in entry["steps"].items():
            agg["steps"][step] = agg["steps"].get(step, 0) + n
    return {"since": since_ms, "until": now_ms, "steps": all_steps, "total": total, "by_source": sources,
            "by_story": [{"story": story, **counts} for story, counts in sorted(by_story.items(), key=lambda kv: -kv[1]["visitors"])]}


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
