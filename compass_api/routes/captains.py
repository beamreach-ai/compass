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
from compass_core import extensions as _extensions
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
    rows = captain_admin.summary(config)
    for row in rows:
        # page: installed from the Captains page (removable there); env: COMPASS_EXTENSIONS; None: shipped
        row["installed_from"] = _extensions.source_of(row["name"]) if not row.get("core") else None
    return {
        "core_version": registry.core_version(),
        "licence": _licence.licence_for(config).to_dict(),
        "captains": rows,
        "extensions": _extensions.state(),
        "incompatible": registry.incompatible(),
        "broken": registry.broken(),
    }


@router.get("/api/captains/catalog")
def get_catalog(request: Request, q: Optional[str] = None, refresh: bool = False) -> Dict[str, Any]:
    """What can be installed: the catalog's entries, each with its status here
    (installed, update, install, incompatible, vendor) and the COMPASS_EXTENSIONS line.
    Declared before /api/captains/{name}, which would otherwise take "catalog" for a name."""
    return _catalog.listing(query=q, config=get_workspace_config(request), refresh=refresh)


class ExtensionPayload(BaseModel):
    name: str


def _ext_error(exc: _extensions.ExtensionsError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": str(exc), **exc.to_dict()})


@router.get("/api/captains/extensions")
def get_extensions(request: Request) -> Dict[str, Any]:
    """What this install has installed and requested, and whether a restart is pending."""
    return _extensions.state()


@router.post("/api/captains/install")
def install_captain(body: ExtensionPayload, request: Request) -> Any:
    """Install a captain from the catalog (or update it to the catalog's version).
    Only catalog entries with a pinned pip line; it takes effect after a restart."""
    ensure_settings_enabled()
    ensure_not_read_only()
    if not _extensions.managed():
        return _ext_error(_extensions.ExtensionsError(
            "extensions_unmanaged",
            "This Compass does not install extensions from the page: add the captain's line to COMPASS_EXTENSIONS and restart.",
        ))
    catalog = _catalog.load()
    entry = next((e for e in (catalog.entries if catalog else []) if e.name == body.name), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{body.name!r} is not in the catalog")
    if not _extensions.allowed(entry.name):
        return _ext_error(_extensions.ExtensionsError(
            "not_allowed", f"{entry.label} is not on this install's allow-list (COMPASS_CATALOG_ALLOW).", status=403,
        ))
    annotated = _catalog.annotate(entry, config=get_workspace_config(request))
    if annotated["status"] not in ("install", "update") or not entry.pip:
        return JSONResponse(status_code=409, content={
            "code": f"not_installable_{annotated['status']}",
            "detail": {
                "installed": f"{entry.label} is already installed at this version.",
                "incompatible": f"{entry.label} needs Compass {entry.requires_core}.",
                "vendor": f"{entry.label} is not a public package; see {entry.homepage}.",
            }.get(annotated["status"], f"{entry.label} cannot be installed from the page."),
        })
    try:
        _extensions.request_install(entry.name, entry.pip, version=entry.version, by=get_actor_ids(request).get("user_id"))
    except _extensions.ExtensionsError as exc:
        return _ext_error(exc)
    return {"captain": entry.name, "version": entry.version, "restart_required": True, "extensions": _extensions.state()}


@router.post("/api/captains/uninstall")
def uninstall_captain(body: ExtensionPayload, request: Request) -> Any:
    """Remove a captain installed from the page; it goes at the next restart.
    Its data and its section of the config stay."""
    ensure_settings_enabled()
    ensure_not_read_only()
    try:
        _extensions.request_uninstall(body.name)
    except _extensions.ExtensionsError as exc:
        return _ext_error(exc)
    return {"captain": body.name, "restart_required": True, "extensions": _extensions.state()}


@router.post("/api/captains/restart", status_code=202)
def restart_compass(request: Request) -> Any:
    """Restart the API and the daemon so they load the new set of extensions.
    Only where the processes run under a restart policy (COMPASS_SELF_RESTART=1)."""
    ensure_settings_enabled()
    ensure_not_read_only()
    if not (_extensions.managed() and _extensions.self_restart()):
        return JSONResponse(status_code=409, content={
            "code": "restart_unmanaged",
            "detail": "This Compass cannot restart itself: run `docker compose up -d` (or restart the api and compass services).",
        })
    at = _extensions.request_restart()
    import os
    import threading

    # answer first, then leave; the restart policy brings the API back, and the
    # entrypoint installs the new set on the way up. compass serve sees the
    # request on its next check and does the same.
    threading.Timer(1.0, os._exit, args=(0,)).start()
    return {"restarting": True, "requested_at": at}


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
