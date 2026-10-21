"""What the dashboard asks about the demo: ``GET /api/demo`` is the mode, the synthetic
world (seeded or not, its rows beside the real scan's), the links, the pinned settings
and the trips in force; ``/api/demo/tour`` is this visitor's tour. Outside demo mode
every route here answers 404: there is no demo to describe."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

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


# ── the tour ────────────────────────────────────────────────────────────────

def _tour_context(request: Request):
    actor = get_actor_ids(request)
    table = dynamo_table(tables(get_workspace_dynamodb_config(request)).items)
    return actor, table, workspace_pk(str(actor.get("workspace_id")))


def _prs(request: Request) -> List[Dict[str, Any]]:
    from compass_core.registry import PRRegistry

    actor = get_actor_ids(request)
    try:
        return PRRegistry.from_config(get_workspace_config(request), workspace_id=actor.get("workspace_id")).list(limit=300)
    except Exception:  # noqa: BLE001 - no registry yet is no pull request yet
        return []


@router.get("/api/demo/tour")
def get_tour(request: Request) -> Dict[str, Any]:
    """This visitor's tour: the seven steps, each done or not, from the row and from the world."""
    require_demo_mode()
    actor, table, pk = _tour_context(request)
    return demo.tour(table, pk, get_workspace_config(request), user_id=actor.get("user_id"), prs=_prs(request))


class TourEvent(BaseModel):
    incident_id: Optional[str] = None


@router.post("/api/demo/tour/{step}")
def post_tour_step(request: Request, step: str, body: TourEvent = TourEvent()) -> Dict[str, Any]:
    """A client-only step happened (a node's detail opened, the IaC tab viewed, a decoy's detail
    opened, the trip in force shown with its incident, a link clicked)."""
    require_demo_mode()
    actor, table, pk = _tour_context(request)
    try:
        demo.mark_tour_step(table, pk, str(actor.get("user_id") or "anonymous"), step, detail={"incident_id": body.incident_id})
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return demo.tour(table, pk, get_workspace_config(request), user_id=actor.get("user_id"), prs=_prs(request))


@router.post("/api/demo/tour/{step}/skip")
def skip_tour_step(request: Request, step: str) -> Dict[str, Any]:
    require_demo_mode()
    actor, table, pk = _tour_context(request)
    try:
        demo.mark_tour_step(table, pk, str(actor.get("user_id") or "anonymous"), step, skipped=True)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return demo.tour(table, pk, get_workspace_config(request), user_id=actor.get("user_id"), prs=_prs(request))
