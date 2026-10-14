"""What a honeytoken record is: class defaults, tags, placement shape, and the item itself.

The product's API and the open `honeypot create` command both build the token
record here, so a token created from either looks the same to the planter,
the alert pipeline and the incident views.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional

from captains.honeypot.classes import HONEYTOKEN_CLASSES, normalize_class
from captains.honeypot.honeytoken_material import default_cover_name, generate_material
from compass_core.keys import honeytoken_sort_key, workspace_pk

# Tags every planted resource carries; the alert pipeline keys on the token id.
MANAGED_TAGS = {
    "beamreach:managed": "true",
    "beamreach:feature": "honeypot",
}

CLASS_DEFAULT_SEVERITY = {
    "s3_object": "high",
    "aws_credential": "critical",
    "db_credential": "critical",
    "api_token": "high",
    "dns_record": "medium",
    "ssh_key": "critical",
}

CLASS_DEFAULT_RUNBOOKS = {
    "s3_object": (
        "1. Record the AWS principal ARN, source IP, and timestamp.\n"
        "2. Determine if the source IP is internal or external.\n"
        "3. If external: escalate to Critical, notify security team, consider blocking IP via WAF.\n"
        "4. If internal: investigate the principal for signs of compromise, check recent activity.\n"
        "5. Rotate any real credentials that may be co-located with this honeytoken."
    ),
    "aws_credential": (
        "1. CRITICAL: Any usage of this key means credentials were stolen.\n"
        "2. Immediately disable the honeytoken key.\n"
        "3. Record the service called, source IP, and requesting principal.\n"
        "4. Identify how the key may have leaked (code scan, git history, logs).\n"
        "5. Treat all co-located credentials as compromised and rotate them.\n"
        "6. Open an incident and notify the security team immediately."
    ),
    "db_credential": (
        "1. CRITICAL: This database login has no legitimate use. Any connection attempt means it was stolen.\n"
        "2. Record the source IP, timestamp, and the client that attempted to connect.\n"
        "3. Identify where the credential was exposed (task definition, env vars, repo, logs).\n"
        "4. Treat all co-located credentials as compromised and rotate them.\n"
        "5. Open an incident and notify the security team immediately."
    ),
    "api_token": (
        "1. Record the source IP, request headers, and any payload.\n"
        "2. Determine if the caller is internal or external.\n"
        "3. Search code repos and logs for where this token may have been exposed.\n"
        "4. Rotate all real tokens of the same type.\n"
        "5. If external caller: report to threat intel team."
    ),
    "dns_record": (
        "1. Record the querying IP and DNS resolver used.\n"
        "2. If internal resolver: investigate source host for lateral movement or malware.\n"
        "3. If external resolver: log as reconnaissance activity, monitor for follow-up access attempts.\n"
        "4. Check if the querying IP appears in other recent security events."
    ),
    "ssh_key": (
        "1. CRITICAL: This key should never be used. Any auth attempt is a confirmed breach indicator.\n"
        "2. Record source IP, timestamp, and SSH client fingerprint.\n"
        "3. Block source IP immediately.\n"
        "4. Scan infrastructure for the origin of the key leak.\n"
        "5. Rotate all SSH keys in affected systems.\n"
        "6. Open an incident and notify the security team immediately."
    ),
}

PLACEMENT_KEYS = (
    "type", "resource_arn", "service_arn", "cluster_arn", "container", "env_vars",
    "tf_address", "repo", "pr_url", "planted_at", "verified_at", "detail", "notes",
)
PLACEMENT_TYPES = ("ecs_task_env", "ecs_task_secret", "secrets_manager", "docker_image", "file", "manual")


def epoch_ms() -> int:
    return int(time.time() * 1000)


def normalize_placement(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Exactly the placement keys, None when unknown or blank."""
    out: Dict[str, Any] = {}
    for key in PLACEMENT_KEYS:
        value = raw.get(key)
        if isinstance(value, str):
            value = value.strip() or None
        if key == "env_vars" and value is not None:
            value = [str(v).strip() for v in value if str(v).strip()] or None
        out[key] = value
    if out.get("type") not in PLACEMENT_TYPES:
        raise ValueError(f"placement.type must be one of {', '.join(PLACEMENT_TYPES)}")
    return out


def action_config(honeytoken_class: str, supplied: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Class defaults when nothing is supplied; a supplied severity is honoured verbatim."""
    if not supplied:
        return {
            "severity": CLASS_DEFAULT_SEVERITY.get(honeytoken_class, "high"),
            "runbook": CLASS_DEFAULT_RUNBOOKS.get(honeytoken_class, ""),
            "notification_endpoint_ids": [],
            "auto_block_ip": False,
        }
    return {
        "severity": supplied.get("severity") or CLASS_DEFAULT_SEVERITY.get(honeytoken_class, "high"),
        "runbook": supplied.get("runbook") or CLASS_DEFAULT_RUNBOOKS.get(honeytoken_class, ""),
        "notification_endpoint_ids": list(supplied.get("notification_endpoint_ids") or []),
        "auto_block_ip": bool(supplied.get("auto_block_ip", False)),
    }


def new_honeytoken_item(
    *,
    workspace_id: str,
    honeytoken_class: str,
    name: str = "",
    description: str = "",
    region: Optional[str] = None,
    account_id: Optional[str] = None,
    resource_path: Optional[str] = None,
    placement: Optional[Dict[str, Any]] = None,
    action_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """A new token record, status pending_pr, with its material generated. Raises ValueError on a bad class."""
    honeytoken_class = normalize_class(honeytoken_class)
    if honeytoken_class not in HONEYTOKEN_CLASSES:
        raise ValueError(f"unknown honeytoken class {honeytoken_class!r}; one of {', '.join(sorted(HONEYTOKEN_CLASSES))}")
    honeytoken_id = str(uuid.uuid4())
    now = epoch_ms()
    name = (name or "").strip() or default_cover_name(honeytoken_id)
    material = generate_material(honeytoken_class=honeytoken_class, name=name, region=region)
    item: Dict[str, Any] = {
        "pk": workspace_pk(workspace_id),
        "sk": honeytoken_sort_key(honeytoken_id),
        "honeytoken_id": honeytoken_id,
        "honeytoken_class": honeytoken_class,
        "name": name,
        "description": (description or "").strip(),
        "status": "pending_pr",
        "created_at": now,
        "updated_at": now,
        "trigger_count": 0,
        "action_config": action_config(honeytoken_class, action_cfg),
        "tags": {
            **MANAGED_TAGS,
            "beamreach:honeytoken-id": honeytoken_id,
            "beamreach:honeytoken-class": honeytoken_class,
        },
        "material": material,
    }
    if account_id:
        item["account_id"] = str(account_id).strip()
    if region:
        item["region"] = str(region).strip()
    if resource_path:
        item["resource_path"] = str(resource_path).strip()
    if placement is not None:
        item["placement"] = normalize_placement(placement)
    return item


def plant_payload(item: Dict[str, Any], *, workspace_id: str, org_id: Optional[str] = None, user_id: Optional[str] = None) -> Dict[str, Any]:
    """The `honeypot.plant` job payload for a token record."""
    return {
        "honeytoken_id": item["honeytoken_id"],
        "honeytoken_class": item["honeytoken_class"],
        "name": item["name"],
        "description": item.get("description", ""),
        "account_id": item.get("account_id"),
        "region": item.get("region"),
        "resource_path": item.get("resource_path"),
        "action_config": item["action_config"],
        "placement": item.get("placement"),
        "workspace_id": workspace_id,
        "org_id": org_id,
        "user_id": user_id,
        "tags": item["tags"],
    }
