"""Existing maps move from DynamoDB to the SQLite file once, on the first start of a
version that has the SQLite store: the ``compass migrate`` step for 0.6.

The copy is one scan into the SQLite store with ``job_id`` ``migration:dynamodb``;
each row keeps the ``last_seen`` the old map recorded as its ``first_seen``. The
DynamoDB table is never touched, so ``COMPASS_MAP_STORE=dynamodb`` (or
``map.store`` back to ``dynamodb``) is the rollback. The step is idempotent: a
workspace already on SQLite, or one whose SQLite file already holds a completed
scan (the daemon started first and scanned), is only marked.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from compass_core.tables import tables
from infra_map.graph_store import backend_name, sqlite_path

logger = logging.getLogger("infra_map.migrate")


def workspace_org(config: Dict[str, Any], workspace_id: str) -> str:
    """The org the map is keyed by: the same fallback chain the API uses."""
    resolved = tables(config)
    return str(config.get("org_id") or config.get("org") or resolved.org_id or workspace_id)


def dynamodb_to_sqlite(config: Dict[str, Any], *, workspace_id: str, session: Any = None, save: Optional[Any] = None) -> Dict[str, Any]:
    """Copy the DynamoDB map into the SQLite file and point the workspace at it.
    ``save(workspace_id, config)`` persists the config; None leaves it to the caller."""
    from infra_map.sql_store import SqliteGraphStore

    org_id = workspace_org(config, workspace_id)
    result: Dict[str, Any] = {"workspace_id": workspace_id, "org_id": org_id, "copied_nodes": 0, "copied_edges": 0}
    if backend_name(config) == "sqlite":
        result["status"] = "already sqlite"
        return result

    path = sqlite_path(config, workspace_id)
    target = SqliteGraphStore(path, workspace_id)
    try:
        if target.list_scans(org_id, limit=1):
            result["status"] = "sqlite already has a scan"
        else:
            table_name = tables(config).map
            if not table_name:
                result["status"] = "no dynamodb map table"
            else:
                from infra_map.dynamo_store import DynamoGraphStore

                graph = DynamoGraphStore(table_name=table_name, workspace_id=workspace_id, session=session).fetch_graph(org_id)
                nodes, edges = graph.get("nodes", []), graph.get("edges", [])
                if nodes or edges:
                    with target.begin_scan(org_id, job_id="migration:dynamodb") as scan:
                        scan.put_nodes(nodes)
                        scan.put_edges(edges)
                    result["copied_nodes"], result["copied_edges"] = len(nodes), len(edges)
                    result["status"] = "copied"
                else:
                    result["status"] = "dynamodb map empty"
    finally:
        target.close()

    section = dict(config.get("map") or {}) if isinstance(config.get("map"), dict) else {}
    section["store"] = "sqlite"
    config["map"] = section
    if save is not None:
        save(workspace_id, config)
    result["path"] = path
    logger.info("map store for %s is sqlite at %s (%s)", workspace_id, path, result["status"])
    return result


def step_0_6_0(config: Dict[str, Any]) -> None:
    """The ``compass migrate`` step: STEPS entry ``("0.6.0-map-sqlite", step_0_6_0)``."""
    from compass_core.config import save_workspace_config

    workspace_id = str(tables(config).workspace_id or config.get("workspace_id") or "default")
    dynamodb_to_sqlite(config, workspace_id=workspace_id, save=save_workspace_config)
