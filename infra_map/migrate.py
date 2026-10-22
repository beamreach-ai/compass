"""Existing maps move from DynamoDB into the map store once, on the first start of a
version that has the SQL stores: the ``compass migrate`` step for 0.6.

The copy goes into the store the workspace names (``map.store``, or ``COMPASS_MAP_STORE``
for the process): the SQLite file for an install that names nothing, the open default;
Postgres for a deployment that set it. It is one scan with ``job_id``
``migration:dynamodb``; each row keeps the ``last_seen`` the old map recorded as its
``first_seen``. The DynamoDB table is never touched, so ``COMPASS_MAP_STORE=dynamodb``
(or ``map.store`` back to ``dynamodb``) is the rollback. The step is idempotent: a store
that already holds a completed scan (the daemon started first and scanned) is only
marked, and the config ends up naming the store either way.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from compass_core.tables import tables
from infra_map.graph_store import backend_name, open_store, sqlite_path

logger = logging.getLogger("infra_map.migrate")


def workspace_org(config: Dict[str, Any], workspace_id: str) -> str:
    """The org the map is keyed by: the same fallback chain the API uses."""
    resolved = tables(config)
    return str(config.get("org_id") or config.get("org") or resolved.org_id or workspace_id)


def dynamodb_to_store(config: Dict[str, Any], *, workspace_id: str, session: Any = None, save: Optional[Any] = None) -> Dict[str, Any]:
    """Copy the DynamoDB map into the store the config names and record that store in the
    config. ``save(workspace_id, config)`` persists the config; None leaves it to the caller."""
    org_id = workspace_org(config, workspace_id)
    result: Dict[str, Any] = {"workspace_id": workspace_id, "org_id": org_id, "copied_nodes": 0, "copied_edges": 0}

    backend = backend_name(config)
    if backend == "dynamodb":
        # nothing chosen: the install predates the SQL stores and gets the open default
        from infra_map.sql_store import SqliteGraphStore

        backend = "sqlite"
        target = SqliteGraphStore(sqlite_path(config, workspace_id), workspace_id)
    else:
        target = open_store(config, workspace_id=workspace_id, session=session)
    result["store"] = backend
    try:
        if target.list_scans(org_id, limit=1):
            result["status"] = f"{backend} already has a scan"
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
    section["store"] = backend
    config["map"] = section
    if save is not None:
        save(workspace_id, config)
    if backend == "sqlite":
        result["path"] = sqlite_path(config, workspace_id)
    logger.info("map store for %s is %s (%s)", workspace_id, backend, result["status"])
    return result


def step_0_6_0(config: Dict[str, Any]) -> None:
    """The ``compass migrate`` step: STEPS entry ``("0.6.0-map-sqlite", step_0_6_0)``."""
    from compass_core.config import save_workspace_config

    workspace_id = str(tables(config).workspace_id or config.get("workspace_id") or "default")
    dynamodb_to_store(config, workspace_id=workspace_id, save=save_workspace_config)
