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
from typing import Any, Dict, List, Tuple

from compass_core.aws import resolve_target_account_id
from compass_core.events import current_job_id
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
