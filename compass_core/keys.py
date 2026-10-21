from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional

PK_NAME = "pk"
SK_NAME = "sk"
DEFAULT_WORKSPACE_ID = "default"


def normalize_workspace_id(workspace_id: Optional[str]) -> str:
    value = str(workspace_id or os.getenv("DEFAULT_WORKSPACE") or os.getenv("TARGET") or "").strip()
    return value or DEFAULT_WORKSPACE_ID


def workspace_pk(workspace_id: Optional[str]) -> str:
    return f"WORKSPACE#{normalize_workspace_id(workspace_id)}"


def workspace_from_config(
    dynamodb_config: Optional[Mapping[str, Any]],
    *,
    explicit: Optional[str] = None,
) -> str:
    if explicit:
        return normalize_workspace_id(explicit)
    if isinstance(dynamodb_config, Mapping):
        candidate = dynamodb_config.get("workspace_id")
        if isinstance(candidate, str) and candidate.strip():
            return normalize_workspace_id(candidate)
    return normalize_workspace_id(None)


def map_graph_prefix(org_id: str) -> str:
    return f"MAP#ORG#{org_id}#"


def map_fact_prefix(org_id: str) -> str:
    return f"MAP#FACT#ORG#{org_id}#"


def map_fact_scope_prefix(org_id: str, source: str, scope: str) -> str:
    return f"{map_fact_prefix(org_id)}SOURCE#{source}#SCOPE#{scope}#"


def map_fact_sort_key(org_id: str, source: str, scope: str, fact_kind: str, fact_id: str) -> str:
    return f"{map_fact_scope_prefix(org_id, source, scope)}KIND#{fact_kind}#FACT#{fact_id}"


def map_node_sort_key(org_id: str, resource_id: str) -> str:
    return f"{map_graph_prefix(org_id)}NODE#{resource_id}"


def map_edge_prefix(org_id: str, from_resource_id: str) -> str:
    return f"{map_graph_prefix(org_id)}REL#FROM#{from_resource_id}#"


def map_edge_sort_key(org_id: str, from_resource_id: str, edge_id: str) -> str:
    return f"{map_edge_prefix(org_id, from_resource_id)}{edge_id}"


def map_target_index_pk(workspace_id: Optional[str], org_id: str, target_resource_id: str) -> str:
    return f"{workspace_pk(workspace_id)}#MAP#ORG#{org_id}#TARGET#{target_resource_id}"


def map_target_index_sk(relationship_type: str, edge_id: str) -> str:
    return f"RELTYPE#{relationship_type}#EDGE#{edge_id}"


def map_type_index_pk(workspace_id: Optional[str], org_id: str, resource_type: str) -> str:
    return f"{workspace_pk(workspace_id)}#MAP#ORG#{org_id}#TYPE#{resource_type}"


def map_type_index_sk(resource_id: str) -> str:
    return resource_id


def aws_inventory_sort_key(org_id: str, resource_id: str) -> str:
    return f"AWS#ORG#{org_id}#RESOURCE#{resource_id}"


def aws_inventory_type_index_pk(workspace_id: Optional[str], org_id: str, resource_type: str) -> str:
    return f"{workspace_pk(workspace_id)}#AWS#ORG#{org_id}#TYPE#{resource_type}"


def tf_inventory_sort_key(org_id: str, resource_address: str) -> str:
    return f"TF#ORG#{org_id}#ADDRESS#{resource_address}"


def tf_inventory_type_index_pk(workspace_id: Optional[str], org_id: str, resource_type: str) -> str:
    return f"{workspace_pk(workspace_id)}#TF#ORG#{org_id}#TYPE#{resource_type}"


def tf_state_sort_key(org_id: str, resource_id: str) -> str:
    return f"TFSTATE#ORG#{org_id}#RESOURCE#{resource_id}"


def audit_finding_sort_key(finding_id: str) -> str:
    return f"FINDING#{finding_id}"


def audit_finding_prefix() -> str:
    return "FINDING#"


def rope_sort_key(rope_id: str) -> str:
    return f"ROPE#{rope_id}"


def rope_prefix() -> str:
    return "ROPE#"


def progress_sort_key(session_id: str, timestamp_ms: int) -> str:
    return f"SESSION#{session_id}#TS#{int(timestamp_ms):013d}"


def progress_session_prefix(session_id: str) -> str:
    return f"SESSION#{session_id}#TS#"


# ── Honeytokens ────────────────────────────────────────────────────────────

def honeytoken_sort_key(honeytoken_id: str) -> str:
    return f"HONEYPOT#TOKEN#{honeytoken_id}"


def honeytoken_prefix() -> str:
    return "HONEYPOT#TOKEN#"


def honeypot_alert_sort_key(honeytoken_id: str, alert_id: str) -> str:
    return f"HONEYPOT#ALERT#TOKEN#{honeytoken_id}#ALERT#{alert_id}"


def honeypot_alert_prefix(honeytoken_id: str) -> str:
    return f"HONEYPOT#ALERT#TOKEN#{honeytoken_id}#ALERT#"


# ── Notification endpoints ─────────────────────────────────────────────────

def notification_endpoint_sort_key(endpoint_id: str) -> str:
    return f"NOTIFY#ENDPOINT#{endpoint_id}"


def notification_endpoint_prefix() -> str:
    return "NOTIFY#ENDPOINT#"


def progress_item(
    *,
    workspace_id: Optional[str],
    session_id: str,
    timestamp_ms: int,
    message: str,
    phase: str,
    status: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        PK_NAME: workspace_pk(workspace_id),
        SK_NAME: progress_sort_key(session_id, timestamp_ms),
        "workspace_id": normalize_workspace_id(workspace_id),
        "session_id": session_id,
        "timestamp": timestamp_ms,
        "message": message,
        "phase": phase,
        "status": status,
    }
    if extra:
        for key, value in extra.items():
            if value is not None:
                item[key] = value
    return item


# ── Honeypot response actions and audits ──
def grant_sort_key(captain: str, grant_id: str) -> str:
    """A grant: one action a captain may run, with its scope and lifecycle (compass_core.grants).
    The decoy captain's whitelist rows are its grants; `0.7.0-grants` renamed their keys."""
    return f"GRANT#{captain}#{grant_id}"


def grant_prefix(captain: Optional[str] = None) -> str:
    return f"GRANT#{captain}#" if captain else "GRANT#"


def honeypot_action_sort_key(action_id: str) -> str:
    return grant_sort_key("honeypot", action_id)


def honeypot_action_prefix() -> str:
    return grant_prefix("honeypot")


def legacy_honeypot_action_prefix() -> str:
    return "HONEYPOT#ACTION#"


def honeypot_audit_sort_key(audit_id: str) -> str:
    """Audit ids start with an epoch-ms timestamp, so a descending query on
    this prefix returns the newest first."""
    return f"HONEYPOT#AUDIT#{audit_id}"


def honeypot_audit_prefix() -> str:
    return "HONEYPOT#AUDIT#"


# ── Honeypot incidents ──
def honeypot_incident_sort_key(incident_id: str) -> str:
    """Incident ids start with an epoch-ms timestamp, so a descending query on
    this prefix returns the newest first."""
    return f"HONEYPOT#INCIDENT#{incident_id}"


def honeypot_incident_prefix() -> str:
    return "HONEYPOT#INCIDENT#"


# ── Pull requests Compass opened ───────────────────────────────────────────

def pr_sort_key(repo: str, number: int) -> str:
    return f"PR#{repo}#{int(number)}"


def pr_prefix() -> str:
    return "PR#"
