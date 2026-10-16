"""``/api/updates``: is a release out, ask the updater for it, roll back, the policy
(docs/open_updates_spec.md §3.2, §4.2, §5). The Compass card on Settings and the
banner above the tabs read this; writes need the admin scope and a writable install."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from compass_api.routes._app_config import ensure_not_read_only, ensure_settings_enabled
from compass_api.routes._workspace import get_actor_ids, get_workspace_config, save_config
from compass_core import updates

router = APIRouter()


def _error(exc: updates.UpdatesError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": str(exc), **exc.to_dict()})


class ApplyPayload(BaseModel):
    target: Optional[str] = None
    force: bool = False
    wait_minutes: int = updates.DEFAULT_WAIT_MINUTES


class PolicyPayload(BaseModel):
    auto: str


@router.get("/api/updates")
def get_updates(request: Request, refresh: bool = False) -> Dict[str, Any]:
    """The state against the feed, the request waiting, the last result, the rollback offer."""
    return updates.status(refresh=refresh, config=get_workspace_config(request))


@router.post("/api/updates/apply", status_code=202)
def apply_update(body: ApplyPayload, request: Request) -> Any:
    """Hand the updater a request for ``target`` (the latest when omitted). 409 with the
    commands when there is no updater; 409 with the reason for a downgrade, a version
    that needs a step in between, or captains that would stop working, unless ``force``."""
    ensure_settings_enabled()
    ensure_not_read_only()
    try:
        record = updates.request_apply(
            body.target, force=body.force, by=get_actor_ids(request).get("user_id"), wait_minutes=body.wait_minutes,
            config=get_workspace_config(request),
        )
    except updates.UpdatesError as exc:
        return _error(exc)
    return {"status": "requested", "apply": "updater", "request": record}


@router.post("/api/updates/rollback", status_code=202)
def rollback_update(request: Request) -> Any:
    """Go back to the version ``rollback.json`` offers (seven days after an update)."""
    ensure_settings_enabled()
    ensure_not_read_only()
    try:
        record = updates.request_rollback(by=get_actor_ids(request).get("user_id"))
    except updates.UpdatesError as exc:
        return _error(exc)
    return {"status": "requested", "apply": "updater", "request": record}


@router.put("/api/updates/policy")
def put_policy(body: PolicyPayload, request: Request) -> Any:
    """``updates.auto`` in the workspace config: notify (default), patch, minor or off;
    mirrored to the volume for the updater."""
    ensure_settings_enabled()
    ensure_not_read_only()
    auto = body.auto.strip().lower()
    if auto not in updates.POLICIES:
        return JSONResponse(status_code=422, content={"detail": f"auto must be one of {', '.join(updates.POLICIES)}"})
    config = get_workspace_config(request)
    section = config.get("updates") if isinstance(config.get("updates"), dict) else {}
    config["updates"] = {**section, "auto": auto}
    save_config(request, config)
    updates.mirror_policy(config)
    return {"policy": updates.policy(config)}
