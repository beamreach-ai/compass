"""The patch captain's commands, for the open dispatcher (compass_core.dispatch): the feeds
and the inventory. The findings and the fix pull requests join here as the captain grows."""

from __future__ import annotations

from typing import Any, Dict

from captains.patch import feed as _feed
from captains.patch import inventory as _inventory
from compass_core import dispatch
from compass_core.workspace import captain_config, resolve


def _config(payload: Dict[str, Any]) -> tuple:
    workspace = resolve(payload)
    config = captain_config(payload, workspace)
    return config, str(workspace.workspace_id)


def feed_run(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run the feeds once, now: what `compass patch feed_run` does the day a disclosure lands."""
    config, workspace_id = _config(payload)
    sources = payload.get("sources")
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]
    return _feed.run(config, workspace_id=workspace_id, sources=sources or None)


def feed_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    config, workspace_id = _config(payload)
    return _feed.status(config, workspace_id=workspace_id)


def feed_inject(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Test-only: put an advisory in the store (an OSV record or the store's own shape)."""
    config, workspace_id = _config(payload)
    advisory = payload.get("advisory")
    if not isinstance(advisory, dict):
        raise ValueError("advisory is required: an OSV record or an advisory dict, as advisory=<json> or in the payload file")
    return _feed.inject(config, workspace_id=workspace_id, advisory=advisory)


def advisories(payload: Dict[str, Any]) -> Dict[str, Any]:
    config, workspace_id = _config(payload)
    rows = _feed.advisories(config, workspace_id=workspace_id, limit=int(payload.get("limit") or 200))
    return {"advisories": rows, "count": len(rows)}


def inventory(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The software on the map as purls, per node, with exposure: what the feeds are asked about."""
    config, workspace_id = _config(payload)
    inv = _inventory.load(config, workspace_id=workspace_id)
    limit = int(payload.get("limit") or 500)
    return {"summary": inv.summary(), "items": [i.to_dict() for i in inv.items[:limit]], "truncated": len(inv.items) > limit}


COMMANDS = {"feed_run": feed_run, "feed_status": feed_status, "feed_inject": feed_inject, "advisories": advisories, "inventory": inventory}

dispatch.register("patch", COMMANDS)
