"""Mapper commands, for the open dispatcher (compass_core.dispatch)."""

from __future__ import annotations

import uuid
from typing import Any, Dict

from compass_core import dispatch
from compass_core.workspace import captain_config, resolve, resolve_target


def run(payload: Dict[str, Any]) -> Dict[str, Any]:
    mode = str(payload.get("mode") or "full").strip().lower()
    if mode != "full":
        raise NotImplementedError("Partial mapper remap is not implemented yet")

    from captains.mapper.captain import MapperCaptain

    workspace = resolve(payload)
    target = resolve_target(payload, workspace)
    config = captain_config(payload, workspace, target=target)
    config["user_id"] = payload.get("user_id") or workspace.user_id
    config["request_id"] = str(payload.get("request_id") or f"mapper-{uuid.uuid4()}")

    try:
        MapperCaptain(config).run()
    except Exception as exc:
        _record_scan(workspace.workspace_id, ok=False, error=str(exc))
        raise
    _record_scan(workspace.workspace_id, ok=True)
    return {"status": "completed", "mode": mode, "target": target, "workspace_id": workspace.workspace_id}


def _record_scan(workspace_id: str, *, ok: bool, error: str = "") -> None:
    """Record the scan in the workspace's infra-scan state: the dashboard enables
    the map-dependent tabs once the first full scan has completed."""
    from compass_api.infra_scan import mark_infra_scan_completed, mark_infra_scan_failed, persist_infra_scan_update
    from compass_core.events import current_job_id

    job_id = current_job_id()
    try:
        if ok:
            persist_infra_scan_update(workspace_id, updater=lambda config: mark_infra_scan_completed(config, job_id=job_id))
        else:
            persist_infra_scan_update(workspace_id, updater=lambda config: mark_infra_scan_failed(config, error=error, job_id=job_id))
    except Exception as exc:  # noqa: BLE001 - the scan itself succeeded or failed already
        import logging

        logging.getLogger("mapper.commands").warning("could not record the scan state: %s", exc)


def export(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Write the map as JSON, Graphviz DOT, or a self-contained HTML viewer.

    payload: format=json|dot|html (default json), out=<path> (default map.<format>),
    org_id (default the workspace's)."""
    from captains.mapper.export import write_map

    workspace = resolve(payload)
    config = captain_config(payload, workspace)
    fmt = str(payload.get("format") or "json").lower()
    out = str(payload.get("out") or f"map.{fmt}")
    org_id = str(payload.get("org_id") or config["org_id"])
    return write_map(config, org_id=org_id, workspace_id=workspace.workspace_id, fmt=fmt, out=out)


COMMANDS = {"run": run, "export": export}

dispatch.register("mapper", COMMANDS)
