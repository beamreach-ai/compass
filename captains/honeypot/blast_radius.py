"""What a task role can reach, computed from its IAM policies and the infra
map, not hardcoded.

Deliberately simple: Action/Resource wildcard matching over Allow statements,
explicit Deny wins, and any statement with a Condition is reported as "may
reach" rather than evaluated. A policy-simulator spot-check on the replay
service is the acceptance test (spec HP-08).
"""

from __future__ import annotations

import fnmatch
import json
from typing import Any, Dict, Iterable, List, Optional, Sequence

# The actions that mean "can read the data", per resource type.
READ_ACTIONS: Dict[str, List[str]] = {
    "secretsmanager_secret": ["secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue"],
    "s3_bucket": ["s3:GetObject", "s3:ListBucket"],
    "rds_instance": ["rds-db:connect", "rds:DescribeDBInstances"],
    "dynamodb_table": ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchGetItem"],
}

CATEGORY: Dict[str, str] = {
    "secretsmanager_secret": "secrets",
    "s3_bucket": "buckets",
    "rds_instance": "databases",
    "dynamodb_table": "tables",
}


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _action_matches(pattern: str, action: str) -> bool:
    return fnmatch.fnmatchcase(action.lower(), pattern.lower())


def _resource_matches(pattern: str, arn: str, resource_type: str) -> bool:
    if pattern == "*":
        return True
    if fnmatch.fnmatchcase(arn, pattern):
        return True
    # Object-level S3 grants (arn:aws:s3:::bucket/*) reach the bucket's data.
    if resource_type == "s3_bucket" and fnmatch.fnmatchcase(f"{arn}/x", pattern):
        return True
    # rds-db:connect uses a dbuser ARN, not the instance ARN; a wildcard on the
    # account's db users counts as reaching every instance.
    if resource_type == "rds_instance" and pattern.startswith("arn:aws:rds-db:"):
        # dbuser ARNs name <resource-id>/<user>; a wildcard in the resource id
        # part reaches every instance in the account.
        return "*" in pattern.rsplit(":", 1)[-1].split("/", 1)[0]
    return False


def _statements(policies: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for policy in policies:
        doc = policy.get("document") if isinstance(policy, dict) and "document" in policy else policy
        if isinstance(doc, str):
            try:
                doc = json.loads(doc)
            except ValueError:
                continue
        if not isinstance(doc, dict):
            continue
        stmts = doc.get("Statement") or []
        if isinstance(stmts, dict):
            stmts = [stmts]
        for s in stmts:
            if isinstance(s, dict):
                out.append(s)
    return out


def reachability(policies: Iterable[Dict[str, Any]], arn: str, resource_type: str) -> Optional[str]:
    """``"reach"``, ``"may reach"`` (an Allow with a Condition), or ``None``."""
    actions = READ_ACTIONS.get(resource_type, [])
    if not actions:
        return None
    verdict: Optional[str] = None
    for stmt in _statements(policies):
        effect = str(stmt.get("Effect", "")).lower()
        stmt_actions = _as_list(stmt.get("Action"))
        not_actions = _as_list(stmt.get("NotAction"))
        resources = _as_list(stmt.get("Resource")) or (["*"] if stmt.get("NotResource") else [])
        hits_action = any(_action_matches(p, a) for p in stmt_actions for a in actions) or (
            bool(not_actions) and not all(any(_action_matches(p, a) for p in not_actions) for a in actions)
        )
        if not hits_action:
            continue
        hits_resource = any(_resource_matches(p, arn, resource_type) for p in resources)
        if not hits_resource:
            continue
        if effect == "deny" and not stmt.get("Condition"):
            return None
        if effect == "allow":
            if stmt.get("Condition"):
                verdict = verdict or "may reach"
            else:
                verdict = "reach"
    return verdict


def compute_blast_radius(
    policies: Sequence[Dict[str, Any]],
    resources: Sequence[Dict[str, Any]],
    *,
    role_arn: str = "",
) -> Dict[str, Any]:
    """``resources`` are map nodes as dicts: ``{resource_id, type, name}``."""
    out: Dict[str, Any] = {
        "role_arn": role_arn,
        "policy_count": len(list(policies)),
        "evaluated": True,
        "method": "policy-evaluation",
    }
    for category in CATEGORY.values():
        out[category] = {"count": 0, "may_reach": 0, "items": []}
    for node in resources:
        rtype = str(node.get("type") or "")
        category = CATEGORY.get(rtype)
        if not category:
            continue
        arn = str(node.get("resource_id") or "")
        verdict = reachability(policies, arn, rtype)
        if verdict is None:
            continue
        entry = {"arn": arn, "name": node.get("name") or arn.rsplit(":", 1)[-1], "certainty": verdict}
        out[category]["items"].append(entry)
        if verdict == "reach":
            out[category]["count"] += 1
        else:
            out[category]["may_reach"] += 1
    out["total"] = sum(out[c]["count"] + out[c]["may_reach"] for c in CATEGORY.values())
    return out


def blast_radius_from_edges(resources: Sequence[Dict[str, Any]], *, role_arn: str = "") -> Dict[str, Any]:
    """Fallback when no policies are known: count what the map links to the
    task (secrets referenced by the task definition, etc.). Labelled so the
    report says where the numbers came from."""
    out = compute_blast_radius([], [], role_arn=role_arn)
    out.update(evaluated=False, method="map-edges")
    for node in resources:
        category = CATEGORY.get(str(node.get("type") or ""))
        if not category:
            continue
        arn = str(node.get("resource_id") or "")
        out[category]["items"].append({"arn": arn, "name": node.get("name") or arn, "certainty": "linked"})
        out[category]["count"] += 1
    out["total"] = sum(out[c]["count"] for c in CATEGORY.values())
    return out
