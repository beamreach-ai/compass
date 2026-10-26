"""``GET /api/inbox``: everything that needs a person, with the routes that act on each
item; snooze and un-snooze (docs/specs/one-inbox.md §3.4).

The list is computed by ``compass_core.inbox`` from the stores and cached for five
seconds per workspace, so the sidebar's poll and a feed's poll cost one computation.
Reads need the ``read`` scope; snoozing is ``operate``. Every other action an item
carries is a route that already exists, with its own scope.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from compass_api.routes._workspace import get_actor_ids, get_workspace_config, get_workspace_dynamodb_config
from compass_core import inbox
from compass_core import updates as _updates
from compass_core.keys import workspace_pk
from compass_core.queue import queue_proxy
from compass_core.tables import dynamo_table, tables

router = APIRouter()
logger = logging.getLogger("compass.api.inbox")
_queue_client = queue_proxy


def _table(request: Request):
    name = tables(get_workspace_dynamodb_config(request)).items
    return dynamo_table(name) if name else None


def context_for(request: Request, *, config: Optional[Dict[str, Any]] = None, workspace_id: Optional[str] = None) -> inbox.Context:
    """The inbox's view of this request's workspace: the items table and the fetchers for
    what lives elsewhere (the jobs table, the PR registry, the update check, the model)."""
    config = config if config is not None else get_workspace_config(request)
    workspace_id = workspace_id or get_actor_ids(request).get("workspace_id")

    def pr_records():
        from compass_core.registry import PRRegistry

        return PRRegistry.from_config(config, workspace_id=workspace_id).list(limit=500)

    def model_available() -> bool:
        from compass_core.llm import model_for

        try:
            return model_for(config) is not None
        except Exception:  # noqa: BLE001 - a misconfigured model is "no model"
            return False

    return inbox.Context(
        config=config, workspace_id=str(workspace_id or ""), pk=workspace_pk(workspace_id), table=_table(request),
        jobs=lambda: _queue_client.list_jobs(limit=inbox.JOBS_WINDOW, workspace_id=workspace_id),
        pr_records=pr_records, update=lambda: _updates.check(config=config).to_dict(), model_available=model_available,
    )


@router.get("/api/inbox")
def get_inbox(request: Request, tab: Optional[str] = None, kind: Optional[str] = None, verb: Optional[str] = None,
              include_snoozed: bool = False, fresh: bool = False) -> Dict[str, Any]:
    if verb and verb not in inbox.VERBS:
        raise HTTPException(status_code=400, detail=f"verb must be one of {', '.join(inbox.VERBS)}")
    return inbox.items(context_for(request), tab=tab, kind=kind, verb=verb, include_snoozed=include_snoozed, fresh=fresh)


@router.post("/api/inbox/{item_id:path}/snooze")
def snooze_item(request: Request, item_id: str) -> Dict[str, Any]:
    """Hide one item until its natural end: only the kinds whose id carries that end."""
    kind = item_id.split(":", 1)[0]
    if kind not in ("job.failed", "update.available") and item_id != "setup.incomplete:model":
        raise HTTPException(status_code=400, detail=f"{kind} items cannot be snoozed: act on them")
    table = _table(request)
    if table is None:
        raise HTTPException(status_code=500, detail="dynamodb_config.items not configured")
    actor = get_actor_ids(request)
    row = inbox.snooze(table, workspace_pk(actor.get("workspace_id")), item_id, by=str(actor.get("user_id") or actor.get("email") or "user"))
    return {"snoozed": True, **row}


@router.delete("/api/inbox/{item_id:path}/snooze")
def unsnooze_item(request: Request, item_id: str) -> Dict[str, Any]:
    table = _table(request)
    if table is None:
        raise HTTPException(status_code=500, detail="dynamodb_config.items not configured")
    inbox.unsnooze(table, workspace_pk(get_actor_ids(request).get("workspace_id")), item_id)
    return {"snoozed": False, "item_id": item_id}
