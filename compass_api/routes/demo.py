"""What the dashboard asks about the demo: ``GET /api/demo`` is the mode, the synthetic
world (seeded or not, its rows beside the real scan's), the links and the pinned
settings from the ``demo`` block. Outside demo mode every route here answers 404:
there is no demo to describe."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from compass_api.routes._infra_map import resolve_infra_map_context
from compass_api.routes._workspace import get_actor_ids, get_workspace_config, get_workspace_dynamodb_config
from compass_core import demo
from compass_core.keys import workspace_pk
from compass_core.tables import dynamo_table, tables
from infra_map import synthetic

router = APIRouter()


def require_demo_mode() -> None:
    if not demo.enabled():
        raise HTTPException(status_code=404, detail="Not a demo: this Compass runs with APP_MODE=production")


@router.get("/api/demo")
def get_demo(request: Request) -> Dict[str, Any]:
    require_demo_mode()
    settings = demo.settings(get_workspace_config(request))
    ctx = resolve_infra_map_context(request)
    store = ctx["store"]
    try:
        counts = store.scopes(ctx["default_org"]) if ctx["default_org"] else {}
    finally:
        store.close()
    world = settings["world"]
    scope = synthetic.world_scope(world["seed"])
    rows = demo.by_scope(counts)
    return {
        "mode": "demo",
        "world": {"resources": world["resources"], "seed": world["seed"], "scope": scope,
                  "seeded": any(r["scope"] == scope for r in rows), "rows": sum(r["rows"] for r in rows if r["synthetic"])},
        "map": rows,
        "links": settings["links"],
        "pinned_settings": settings["pinned_settings"],
        "cooldown_seconds": settings["cooldown_seconds"],
        "trippable": list(settings["trippable"]),
        "cooldowns": _cooldowns(request),
    }


def _cooldowns(request: Request):
    """Every trippable decoy's last trip, as the visitor may see it."""
    actor = get_actor_ids(request)
    table = dynamo_table(tables(get_workspace_dynamodb_config(request)).items)
    now = demo.epoch_ms()
    me = str(actor.get("user_id") or "anonymous")
    return [demo.public_cooldown(row, me=me, now_ms=now) for row in demo.cooldowns(table, workspace_pk(str(actor.get("workspace_id"))), now_ms=now)]
