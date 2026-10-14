"""PR watcher commands, for the open dispatcher (compass_core.dispatch)."""

from __future__ import annotations

from typing import Any, Dict

from compass_core import dispatch
from compass_core.workspace import captain_config, resolve, resolve_target


def _captain(payload: Dict[str, Any]):
    from captains.prs.captain import PRCaptain

    workspace = resolve(payload)
    target = resolve_target(payload, workspace)
    return PRCaptain(config=captain_config(payload, workspace), workspace_ctx=workspace, target=target)


def address(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _captain(payload).address(payload)


def sync(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _captain(payload).sync(payload)


COMMANDS = {"address": address, "sync": sync}

dispatch.register("prs", COMMANDS)
