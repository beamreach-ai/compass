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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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


def _grants(config: Dict[str, Any]) -> None:
    """0.7.0: the decoy captain's whitelist rows are grants (compass_core.grants): the key moves
    from HONEYPOT#ACTION#<id> to GRANT#honeypot#<id> and the row gets a lifecycle. An enabled row
    whose responder role was applied is `applied`: the next verification tick arms it once IAM
    holds and allows its statements; a disabled row is `revoked`. Nothing runs until then."""
    from boto3.dynamodb.conditions import Key

    from compass_core.keys import grant_sort_key, legacy_honeypot_action_prefix

    name = tables(config).items
    workspace_id = str(config.get("workspace_id") or "")
    if not name or not workspace_id:
        return
    table = dynamo_table(name)
    pk = workspace_pk(workspace_id)
    kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(legacy_honeypot_action_prefix())}
    moved = 0
    while True:
        page = table.query(**kwargs)
        for item in page.get("Items", []):
            row = dict(item)
            grant_id = str(row.get("action_id") or str(row["sk"]).rsplit("#", 1)[-1])
            enabled = bool(row.get("enabled", True)) and str(row.get("mode") or "off") != "off"
            row.update(sk=grant_sort_key("honeypot", grant_id), action_id=grant_id, captain="honeypot",
                       grant_status="applied" if enabled else "revoked", grant_pr=row.get("grant_pr"), applied_at=None, verified_at=None,
                       policy_hash=None, grant_history=[{"from": None, "to": "applied" if enabled else "revoked", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "by": "migration 0.7.0"}])
            table.put_item(Item=row)
            moved += 1
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    if moved:
        logger.info("moved %d decoy whitelist rows to grants", moved)


# 0.6.0: the map moves from DynamoDB to the SQLite file (infra_map.migrate); the table stays for rollback
# 0.7.0: the aws block names roles, not credentials; `compass connect` creates them; the whitelist rows become grants
STEPS: List[Step] = [("0.6.0-map-sqlite", _map_to_sqlite), ("0.7.0-access-model", _access_model), ("0.7.0-grants", _grants)]


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


def run(config: Dict[str, Any], *, workspace_id: str, client: Any = None, table: Any = None, steps: Optional[List[Step]] = None,
        rerun: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Create missing tables, then run every step not yet recorded, in order. ``rerun`` names
    recorded steps to run again (an operator's call after a step ran against the wrong
    store or saved nothing); every step is safe to repeat."""
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
        done = set(applied(config, workspace_id=workspace_id, table=table)) - {str(v) for v in (rerun or [])}
        for version, step in todo:
            if version in done:
                continue
            logger.info("running migration %s for workspace %s", version, workspace_id)
            # the steps read the id from the config; the file loader sets it, a host's loader may not
            step({**config, "workspace_id": workspace_id})
            table.put_item(Item={"pk": workspace_pk(workspace_id), "sk": sort_key(version), "version": version,
                                 "core": running_version(), "at": int(time.time() * 1000)})
            ran.append(version)
    return {"workspace_id": workspace_id, "core": running_version(), **created, "migrations": ran}
