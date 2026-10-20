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


def _access_model(config: Dict[str, Any]) -> None:
    """0.7.0: the `aws` block names roles, not credentials (docs/specs/access-model.md §12).
    An install that assumed roles keeps them as `roles.read` / `roles.act`; one that used
    the profile (`local_iam`) or static keys gets no roles and the Connect card, and runs
    nothing against a target until `compass connect` has run."""
    from compass_core.config import save_workspace_config

    aws = config.get("aws") if isinstance(config.get("aws"), dict) else None
    if not aws or isinstance(aws.get("roles"), dict):
        return
    roles: Dict[str, str] = {}
    for key, kind in (("assume_role_read", "read"), ("assume_role", "read"), ("assume_role_respond", "act")):
        legacy = aws.get(key)
        if isinstance(legacy, dict) and legacy.get("account_id") and legacy.get("role_name") and kind not in roles:
            roles[kind] = f"arn:aws:iam::{legacy['account_id']}:role/{legacy['role_name']}"
            if legacy.get("external_id") and not aws.get("external_id"):
                aws["external_id"] = str(legacy["external_id"])
            aws.setdefault("account_id", str(legacy["account_id"]))
    if aws.get("generated_external_id") and not aws.get("external_id"):
        aws["external_id"] = str(aws["generated_external_id"])
    aws["roles"] = roles
    aws["migrated_from"] = aws.get("auth_mode") or ("assume_role" if roles else ("access_key" if aws.get("credentials") else None))
    for stale in ("auth_mode", "assume_role", "assume_role_read", "assume_role_write", "assume_role_respond", "credentials",
                  "credentials_read", "credentials_write", "credentials_respond", "static_credentials", "generated_external_id"):
        aws.pop(stale, None)
    workspace_id = str(config.get("workspace_id") or "")
    if workspace_id:
        save_workspace_config(workspace_id, config)


# 0.6.0: the map moves from DynamoDB to the SQLite file (infra_map.migrate); the table stays for rollback
# 0.7.0: the aws block names roles, not credentials; `compass connect` creates them
STEPS: List[Step] = [("0.6.0-map-sqlite", _map_to_sqlite), ("0.7.0-access-model", _access_model)]


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
