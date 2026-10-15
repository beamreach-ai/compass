"""The example captain's commands. Importing this module registers them."""

from __future__ import annotations

from typing import Any, Dict

from compass_core import dispatch
from compass_core.workspace import resolve


def greet(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Say hello to name= with the configured greeting."""
    workspace = resolve(payload)
    section = workspace.config.get("hello") if isinstance(workspace.config.get("hello"), dict) else {}
    return {"greeting": f"{section.get('greeting', 'hello')}, {payload.get('name', 'world')}", "workspace_id": workspace.workspace_id}


COMMANDS = {"greet": greet}

dispatch.register("hello", COMMANDS)
