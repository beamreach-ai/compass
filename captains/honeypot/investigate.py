"""The investigation step of an incident: pin the source task, find its role,
compute what that role can reach, and record it on the incident.

Runs as a honeypot job right after the first alert of an incident has been
sent, so the Slack message is never delayed by AWS lookups.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from captains.honeypot import incidents
from captains.honeypot.blast_radius import blast_radius_from_edges, compute_blast_radius
from captains.honeypot.correlation import correlate
from compass_core.tables import tables

logger = logging.getLogger("honeypot-investigate")

DATA_NODE_TYPES = ("secretsmanager_secret", "s3_bucket", "rds_instance", "dynamodb_table")


def load_map_store_for_config(config: Dict[str, Any], workspace_id: str) -> Tuple[Optional[Any], str]:
    """The infra map store for a workspace, from its config alone (jobs have no
    request). Returns (store, org_id); (None, "") when the map is not configured."""
    from infra_map.store import InfraMapStore

    config = config or {}
    resolved = tables(config)
    table_name = resolved.map
    org_id = str(config.get("org_id") or config.get("org") or resolved.org_id or workspace_id or "")
    if not table_name:
        return None, org_id
    try:
        return InfraMapStore(table_name=table_name, workspace_id=workspace_id), org_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("map store unavailable: %s", exc)
        return None, org_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def role_policies_from_map(store: Any, org_id: str, role_arn: str) -> List[Dict[str, Any]]:
    node = store.get_resource(org_id, role_arn) if store and role_arn else None
    attrs = getattr(node, "attributes", None) or {}
    return list(attrs.get("policies") or [])


def role_policies_live(session: Any, role_arn: str) -> List[Dict[str, Any]]:
    """Read the policies straight from IAM when the map has none (the map may
    predate the role, or policy collection may have been skipped)."""
    iam = session.client("iam")
    role_name = role_arn.rsplit("/", 1)[-1]
    policies: List[Dict[str, Any]] = []
    for page in iam.get_paginator("list_role_policies").paginate(RoleName=role_name):
        for name in page.get("PolicyNames", []):
            policies.append({"name": name, "source": "inline", "document": iam.get_role_policy(RoleName=role_name, PolicyName=name)["PolicyDocument"]})
    for page in iam.get_paginator("list_attached_role_policies").paginate(RoleName=role_name):
        for attached in page.get("AttachedPolicies", []):
            arn = attached["PolicyArn"]
            version = iam.get_policy(PolicyArn=arn)["Policy"]["DefaultVersionId"]
            policies.append({"name": attached.get("PolicyName"), "source": "managed", "arn": arn, "document": iam.get_policy_version(PolicyArn=arn, VersionId=version)["PolicyVersion"]["Document"]})
    return policies


def data_resources_from_map(store: Any, org_id: str) -> List[Dict[str, Any]]:
    """Every secret, bucket, database and table the map knows, as plain dicts."""
    out: List[Dict[str, Any]] = []
    if store is None:
        return out
    graph = store.fetch_graph(org_id) if hasattr(store, "fetch_graph") else {"nodes": []}
    for node in graph.get("nodes", []):
        ntype = getattr(node, "type", None) or (node.get("type") if isinstance(node, dict) else None)
        if ntype not in DATA_NODE_TYPES:
            continue
        out.append({
            "resource_id": getattr(node, "resource_id", None) or node.get("resource_id"),
            "type": ntype,
            "name": getattr(node, "name", None) or node.get("name"),
        })
    return out


def region_of_arns(*arns: Optional[str]) -> Optional[str]:
    for arn in arns:
        parts = str(arn or "").split(":")
        if len(parts) > 4 and parts[0] == "arn" and parts[3]:
            return parts[3]
    return None


def data_resources_live(session: Any, region: str) -> List[Dict[str, Any]]:
    """Secrets, buckets, RDS instances and DynamoDB tables straight from the
    account (names and ARNs only), for when the map has none."""
    out: List[Dict[str, Any]] = []
    sm = session.client("secretsmanager", region_name=region)
    for page in sm.get_paginator("list_secrets").paginate():
        for sec in page.get("SecretList", []):
            out.append({"resource_id": sec["ARN"], "type": "secretsmanager_secret", "name": sec.get("Name")})
    s3 = session.client("s3", region_name=region)
    for b in s3.list_buckets().get("Buckets", []):
        out.append({"resource_id": f"arn:aws:s3:::{b['Name']}", "type": "s3_bucket", "name": b["Name"]})
    rds = session.client("rds", region_name=region)
    for page in rds.get_paginator("describe_db_instances").paginate():
        for db in page.get("DBInstances", []):
            out.append({"resource_id": db["DBInstanceArn"], "type": "rds_instance", "name": db.get("DBInstanceIdentifier")})
    ddb = session.client("dynamodb", region_name=region)
    account = None
    for page in ddb.get_paginator("list_tables").paginate():
        for name in page.get("TableNames", []):
            if account is None:
                account = session.client("sts", region_name=region).get_caller_identity()["Account"]
            out.append({"resource_id": f"arn:aws:dynamodb:{region}:{account}:table/{name}", "type": "dynamodb_table", "name": name})
    return out


def linked_resources_from_map(store: Any, org_id: str, task_definition_arn: Optional[str]) -> List[Dict[str, Any]]:
    if store is None or not task_definition_arn:
        return []
    found = store.neighbors(org_id, task_definition_arn, depth=1, direction="incoming")
    out = []
    for node in found.get("nodes", []):
        if getattr(node, "type", None) in DATA_NODE_TYPES:
            out.append({"resource_id": node.resource_id, "type": node.type, "name": node.name})
    return out


def investigate_incident(
    *,
    table: Any,
    pk: str,
    incident_id: str,
    session: Any,
    region: str,
    store: Any = None,
    org_id: str = "",
) -> Dict[str, Any]:
    incident = incidents.get_incident(table, pk, incident_id)
    if not incident:
        raise KeyError(f"incident {incident_id} not found")
    placement = incident.get("placement") or {}
    source_ip = incident.get("source_ip")
    # The workload lives where its ARNs say, not where the workspace default
    # points (the control plane and the honeytokens are in different regions).
    region = region_of_arns(placement.get("service_arn"), placement.get("cluster_arn"), incident.get("service_arn")) or region

    correlation = correlate(
        session,
        region=region,
        source_ip=source_ip,
        cluster_arns=[placement.get("cluster_arn")] if placement.get("cluster_arn") else [],
        placement=placement,
    )

    role_arn = correlation.get("task_role_arn") or ""
    task = correlation.get("task") or {}
    if not role_arn and store is not None and task.get("task_definition_arn"):
        td = store.get_resource(org_id, task["task_definition_arn"])
        role_arn = ((getattr(td, "attributes", None) or {}).get("taskRoleArn")) or ""
        correlation["task_role_arn"] = role_arn or None

    policies: List[Dict[str, Any]] = role_policies_from_map(store, org_id, role_arn) if role_arn else []
    if not policies and role_arn and session is not None:
        try:
            policies = role_policies_live(session, role_arn)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read policies for %s live: %s", role_arn, exc)

    if policies:
        resources = data_resources_from_map(store, org_id)
        if session is not None:
            # The map may predate the workload (or the scan may be stale), so
            # merge in what the account holds right now, by ARN.
            try:
                known = {r["resource_id"] for r in resources}
                resources += [r for r in data_resources_live(session, region) if r["resource_id"] not in known]
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not list data resources live: %s", exc)
        blast = compute_blast_radius(policies, resources, role_arn=role_arn)
    else:
        blast = blast_radius_from_edges(linked_resources_from_map(store, org_id, task.get("task_definition_arn")), role_arn=role_arn)

    fields: Dict[str, Any] = {
        "correlation": correlation,
        "blast_radius": blast,
        "investigation_done_at": _now(),
    }
    if task.get("task_id"):
        fields["source_task_id"] = task["task_id"]
    if task.get("cluster_arn"):
        fields["cluster_arn"] = task["cluster_arn"]
    if correlation.get("service_arn"):
        fields["service_arn"] = correlation["service_arn"]
    incidents.mark(table, pk, incident_id, **fields)
    return {"incident_id": incident_id, **fields}
