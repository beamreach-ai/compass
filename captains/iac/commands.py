"""Code-as-cloud commands, for the open dispatcher (compass_core.dispatch)."""

from __future__ import annotations

from typing import Any, Dict

from compass_core import dispatch
from compass_core.workspace import captain_config, resolve, resolve_target


def _captain(payload: Dict[str, Any]):
    from captains.iac.captain import IACCaptain

    workspace = resolve(payload)
    target = resolve_target(payload, workspace)
    config = captain_config(payload, workspace, target=target)
    return IACCaptain(org_id=config["org_id"], target=target, config=config, workspace_ctx=workspace)


def import_(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _captain(payload).run_import(
        resource_ids=payload.get("resource_ids") or [],
        repo=payload["repo"],
        branch=payload.get("branch", "main"),
        module_path=payload.get("module_path"),
        is_new_module=bool(payload.get("is_new_module", False)),
    )


def fix_drift(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _captain(payload).run_fix_drift(resource_ids=payload.get("resource_ids") or [])


COMMANDS = {"import": import_, "fix_drift": fix_drift}

dispatch.register("iac", COMMANDS)
