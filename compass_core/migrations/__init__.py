"""What a new version does to the workspace on first start: ``compass migrate``.

``run()`` creates the tables that are missing (``init_tables``, idempotent) and
then the steps listed in ``STEPS``, each once per workspace: a step is
``(version, callable(config))`` and its run is recorded in the items table as
``MIGRATION#<version>``, so the next start skips it. The entrypoint runs this
for ``serve`` and ``api`` under the extensions lock, so the two containers,
which start together, do not race.

A step must be safe to run against data the newer code already wrote (the daemon
may have started first) and must never delete.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from compass_core.keys import workspace_pk
from compass_core.tables import dynamo_table, tables

logger = logging.getLogger("compass.migrations")

Step = Tuple[str, Callable[[Dict[str, Any]], None]]


def _map_to_sqlite(config: Dict[str, Any]) -> None:
    from infra_map.migrate import step_0_6_0

    step_0_6_0(config)


# 0.6.0: the map moves from DynamoDB to the SQLite file (infra_map.migrate); the table stays for rollback
STEPS: List[Step] = [("0.6.0-map-sqlite", _map_to_sqlite)]


def sort_key(version: str) -> str:
    return f"MIGRATION#{version}"


def applied(config: Dict[str, Any], *, workspace_id: str, table: Any = None) -> List[str]:
    """The step versions already recorded for the workspace, oldest first."""
    name = tables(config).items
    if not name:
        return []
    table = table or dynamo_table(name)
    from boto3.dynamodb.conditions import Key

    found: List[str] = []
    kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(workspace_pk(workspace_id)) & Key("sk").begins_with("MIGRATION#")}
    while True:
        page = table.query(**kwargs)
        found += [str(item["sk"]).split("#", 1)[1] for item in page.get("Items", [])]
        if not page.get("LastEvaluatedKey"):
            return found
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def run(config: Dict[str, Any], *, workspace_id: str, client: Any = None, table: Any = None, steps: Optional[List[Step]] = None) -> Dict[str, Any]:
    """Create missing tables, then run every step not yet recorded, in order."""
    from compass_core.commands import create_tables
    from compass_core.updates import running_version

    created = create_tables(config, client=client)
    name = tables(config).items
    ran: List[str] = []
    todo = list(STEPS if steps is None else steps)
    if todo and not name:
        raise LookupError("no items table in the workspace config to record migrations in (dynamodb_config.items)")
    if todo:
        table = table or dynamo_table(name)
        done = set(applied(config, workspace_id=workspace_id, table=table))
        for version, step in todo:
            if version in done:
                continue
            logger.info("running migration %s for workspace %s", version, workspace_id)
            step(config)
            table.put_item(Item={"pk": workspace_pk(workspace_id), "sk": sort_key(version), "version": version,
                                 "core": running_version(), "at": int(time.time() * 1000)})
            ran.append(version)
    return {"workspace_id": workspace_id, "core": running_version(), **created, "migrations": ran}
