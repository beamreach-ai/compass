from __future__ import annotations

import logging
import os
from typing import Any, Dict

from fastapi import Request

from compass_api.routes._workspace import (
    get_workspace_config,
    get_workspace_context,
    get_workspace_dynamodb_config,
)
from compass_core.config import workspace_config as load_target_config
from compass_core.tables import tables
from infra_map.graph_store import open_store

logger = logging.getLogger("infra-map-routes")


def _load_legacy_map_store() -> Dict[str, Any]:
    target = os.getenv("TARGET")
    config = load_target_config(target)
    default_org = (
        config.get("org_id")
        or config.get("org")
        or os.getenv("ORG_ID")
        or (target.split("/")[-1] if target else None)
    )
    if not default_org:
        raise RuntimeError("Unable to determine default org_id for infra map queries")

    store = open_store(config, workspace_id=target or default_org)
    return {"store": store, "default_org": default_org}


def resolve_infra_map_context(request: Request) -> Dict[str, Any]:
    try:
        ctx = get_workspace_context(request)
        config = get_workspace_config(request)
        resolved = tables(get_workspace_dynamodb_config(request))
        default_org = (
            ctx.org_id
            or config.get("org_id")
            or config.get("org")
            or resolved.org_id
            or ctx.workspace_id
        )
        store = open_store({**config, "dynamodb_config": get_workspace_dynamodb_config(request)}, workspace_id=ctx.workspace_id)
        return {
            "store": store,
            "default_org": str(default_org).strip() if default_org else "",
            "workspace_id": ctx.workspace_id,
        }
    except Exception:
        logger.debug("Falling back to legacy target-bound map store", exc_info=True)
        legacy = _load_legacy_map_store()
        return {
            "store": legacy["store"],
            "default_org": legacy["default_org"],
            "workspace_id": os.getenv("TARGET") or legacy["default_org"],
        }
