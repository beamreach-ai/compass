"""The Captains page's API (docs/captain_extensions_spec.md §3): what is installed,
on or off, licensed, and each captain's configuration."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from compass_api.routes._app_config import ensure_not_read_only, ensure_settings_enabled
from compass_api.routes._workspace import get_actor_ids, get_workspace_config, save_config
from compass_core import captain_admin
from compass_core import captains as registry
from compass_core import catalog as _catalog
from compass_core import licence as _licence

router = APIRouter()


def _error(exc: captain_admin.CaptainAdminError) -> JSONResponse:
    """A flat body: ``detail`` for every client, ``code`` and ``captains`` for the ones that switch on it."""
    return JSONResponse(status_code=exc.status, content={"detail": str(exc), **exc.to_dict()})


class TogglePayload(BaseModel):
    with_dependencies: bool = False
    cascade: bool = False


@router.get("/api/captains")
def list_captains(request: Request) -> Dict[str, Any]:
    config = get_workspace_config(request)
    return {
        "core_version": registry.core_version(),
        "licence": _licence.licence_for(config).to_dict(),
        "captains": captain_admin.summary(config),
        "incompatible": registry.incompatible(),
        "broken": registry.broken(),
    }


@router.get("/api/captains/catalog")
def get_catalog(request: Request, q: Optional[str] = None, refresh: bool = False) -> Dict[str, Any]:
    """What can be installed: the catalog's entries, each with its status here
    (installed, update, install, incompatible, vendor) and the COMPASS_EXTENSIONS line.
    Declared before /api/captains/{name}, which would otherwise take "catalog" for a name."""
    return _catalog.listing(query=q, config=get_workspace_config(request), refresh=refresh)


@router.get("/api/captains/{name}")
def get_captain(name: str, request: Request) -> Any:
    """One captain's state, plus its commands (name → one line) for the generic page."""
    try:
        state = captain_admin.state_dict(get_workspace_config(request), name)
    except captain_admin.CaptainAdminError as exc:
        return _error(exc)
    state["commands"] = registry.commands_of(name) if state.get("compatible") else {}
    return state


def _toggle(name: str, request: Request, payload: TogglePayload, enabled: bool) -> Any:
    ensure_settings_enabled()
    ensure_not_read_only()
    config = get_workspace_config(request)
    try:
        changed = captain_admin.set_enabled(
            config, name, enabled,
            with_dependencies=payload.with_dependencies, cascade=payload.cascade,
            actor=get_actor_ids(request).get("user_id"),
        )
    except captain_admin.CaptainAdminError as exc:
        return _error(exc)
    save_config(request, config)
    return {"changed": changed, "captain": captain_admin.state_dict(config, name)}


@router.post("/api/captains/{name}/enable")
def enable_captain(name: str, request: Request, payload: TogglePayload = Body(default_factory=TogglePayload)) -> Any:
    return _toggle(name, request, payload, True)


@router.post("/api/captains/{name}/disable")
def disable_captain(name: str, request: Request, payload: TogglePayload = Body(default_factory=TogglePayload)) -> Any:
    return _toggle(name, request, payload, False)


@router.get("/api/captains/{name}/config")
def get_captain_config(name: str, request: Request) -> Dict[str, Any]:
    manifest = registry.manifest(name)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"no captain named {name!r} is installed")
    return captain_admin.describe_config(get_workspace_config(request), manifest)


@router.put("/api/captains/{name}/config")
def put_captain_config(name: str, request: Request, payload: Dict[str, Any] = Body(default={}), replace: bool = False) -> Any:
    ensure_settings_enabled()
    ensure_not_read_only()
    manifest = registry.manifest(name)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"no captain named {name!r} is installed")
    config = get_workspace_config(request)
    try:
        captain_admin.apply_section_update(config, manifest, payload or {}, replace=replace)
    except captain_admin.CaptainAdminError as exc:
        return _error(exc)
    save_config(request, config)
    return captain_admin.describe_config(config, manifest)
