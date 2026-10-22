from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from boto3.dynamodb.conditions import Key
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from captains.honeypot.captain import discover_map_repos
from captains.honeypot.catalog import CLASS_DEFAULT_RUNBOOKS, CLASS_DEFAULT_SEVERITY, MANAGED_TAGS  # noqa: F401 - re-exported
from captains.honeypot.classes import HONEYTOKEN_CLASSES, LEGACY_CLASS_ALIASES, normalize_class  # noqa: F401
from captains.honeypot.honeytoken_material import default_cover_name, generate_material
from captains.honeypot.ingestion import record_alert
from captains.honeypot.placement import get_placement, placement_summary, to_native
from compass_api.routes._app_config import write_guard
from compass_api.routes._infra_map import resolve_infra_map_context
from compass_api.routes._workspace import (
    get_actor_ids,
    get_workspace_config,
    get_workspace_dynamodb_config,
    save_config,
)
from compass_core import sessions
from compass_core.keys import (
    honeypot_alert_prefix,
    honeypot_alert_sort_key,
    honeytoken_prefix,
    honeytoken_sort_key,
    workspace_pk,
)
from compass_core.queue import queue_proxy
from compass_core.tables import dynamo_table, tables

# Every write on the honeypot routers is refused on a read-only stand (the demo
# audience browses from their phones); the inbound alert path stays open.
router = APIRouter(dependencies=[Depends(write_guard("/api/honeypots/inbound-alert"))])
logger = logging.getLogger("honeypots-api")
_queue_client = queue_proxy

# The token record's vocabulary lives with the honeypot package (captains/honeypot/catalog.py);
# these names stay for the other routes.

# Public-exposure signals in infra map resource types that warrant honeytoken placement.
# Types match what aws_mapper.py writes (not Terraform type names).
PUBLIC_RESOURCE_TYPES = {
    "lambda_function":          ("api_token",   "high"),
    "ec2_instance":             ("ssh_key",      "high"),
    "iam_role":                 ("aws_credential",   "high"),
    "iam_user":                 ("aws_credential",   "critical"),
    "secretsmanager_secret":    ("aws_credential",   "critical"),
    "elbv2_load_balancer":      ("api_token",    "high"),
    "dynamodb_table":           ("api_token",    "medium"),
    "ecr_repository":           ("api_token",    "medium"),
    "s3_bucket":                ("s3_object",    "high"),
    "route53_record":           ("dns_record",   "medium"),
}


def _epoch_ms() -> int:
    return int(time.time() * 1000)


# The placement helpers live with the honeypot package (captains/honeypot/placement.py);
# the other honeypot routes import them from here under their old names.
_to_native = to_native
_get_placement = get_placement
_placement_summary = placement_summary


def _resolve_table(request: Request):
    table_name = tables(get_workspace_dynamodb_config(request)).items
    if not table_name:
        raise HTTPException(status_code=500, detail="dynamodb_config.items not configured")
    return dynamo_table(table_name)


def _ws_pk(request: Request) -> str:
    actor = get_actor_ids(request)
    return workspace_pk(actor.get("workspace_id"))


def _count_active_alerts(table, pk: str, honeytoken_id: str) -> int:
    try:
        resp = table.query(
            KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(honeypot_alert_prefix(honeytoken_id)),
            FilterExpression="attribute_not_exists(resolved_at)",
        )
        return resp.get("Count", 0)
    except Exception:
        return 0


def _normalize_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Bring a stored item up to the current field names and class ids.

    Items written before the rename carry `asset_id`, `asset_class` and `lure`;
    reading them through here keeps them working until the migration script has
    been applied.
    """
    out = dict(item)
    for old, new in (("asset_id", "honeytoken_id"), ("asset_class", "honeytoken_class"), ("lure", "material")):
        if old in out:
            legacy = out.pop(old)
            out.setdefault(new, legacy)
    if "honeytoken_class" in out:
        out["honeytoken_class"] = normalize_class(out["honeytoken_class"])
    return out


def _serialize_honeytoken(item: Dict[str, Any], active_alert_count: int = 0) -> Dict[str, Any]:
    raw = _to_native(_normalize_item(item))
    raw.pop("pk", None)
    raw.pop("sk", None)
    # Material is revealed only via the dedicated /material endpoint, never in
    # list/detail payloads.
    raw.pop("material", None)
    raw["active_alert_count"] = active_alert_count
    raw["placement"] = raw.get("placement") or None
    return raw


def _serialize_alert(item: Dict[str, Any]) -> Dict[str, Any]:
    raw = _to_native(_normalize_item(item))
    raw.pop("pk", None)
    raw.pop("sk", None)
    return raw



# ── Models ─────────────────────────────────────────────────────────────────

class ActionConfigPayload(BaseModel):
    severity: str = Field(default="high", pattern="^(critical|high|medium|low)$")
    runbook: str = Field(default="")
    notification_endpoint_ids: List[str] = Field(default_factory=list)
    auto_block_ip: bool = Field(default=False)


class CreateHoneytokenPayload(BaseModel):
    honeytoken_class: str = Field(..., description="One of: aws_credential, db_credential, ssh_key, s3_object, api_token, dns_record")
    name: str = Field(default="", description="Cover name; empty picks one from the realistic pool")
    description: str = Field(default="")
    account_id: Optional[str] = None
    region: Optional[str] = None
    resource_path: Optional[str] = None
    action_config: Optional[ActionConfigPayload] = None
    placement: Optional[PlacementPayload] = None


class UpdateHoneytokenPayload(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    account_id: Optional[str] = None
    region: Optional[str] = None
    resource_path: Optional[str] = None
    status: Optional[str] = None
    action_config: Optional[ActionConfigPayload] = None


class AutopilotSuggestionPayload(BaseModel):
    resource_id: str
    recommended_class: str
    resource_name: Optional[str] = None
    account_id: Optional[str] = None
    region: Optional[str] = None
    description: Optional[str] = None
    default_config: Optional[Dict[str, Any]] = None


class AutopilotPayload(BaseModel):
    suggestions: List[AutopilotSuggestionPayload]
    notification_endpoint_ids: List[str] = Field(default_factory=list)


class ExecuteRunbookPayload(BaseModel):
    runbook: str = Field(default="")
    notification_endpoint_ids: List[str] = Field(default_factory=list)


PLACEMENT_TYPES = ("ecs_task_env", "ecs_task_secret", "secrets_manager", "docker_image", "file", "manual")
PLACEMENT_KEYS = (
    "type", "resource_arn", "service_arn", "cluster_arn", "container", "env_vars",
    "tf_address", "repo", "pr_url", "planted_at", "verified_at", "detail", "notes",
)


class PlacementPayload(BaseModel):
    """Where a honeytoken physically is. A token has exactly one."""
    type: str = Field(..., pattern="^(ecs_task_env|ecs_task_secret|secrets_manager|docker_image|file|manual)$")
    resource_arn: Optional[str] = None
    service_arn: Optional[str] = None
    cluster_arn: Optional[str] = None
    container: Optional[str] = None
    env_vars: Optional[List[str]] = None
    tf_address: Optional[str] = None
    repo: Optional[str] = None
    pr_url: Optional[str] = None
    planted_at: Optional[Union[int, str]] = None
    verified_at: Optional[Union[int, str]] = None
    detail: Optional[str] = None
    notes: Optional[str] = None


def _placement_to_item(body: PlacementPayload) -> Dict[str, Any]:
    """Exactly the placement keys, None when unknown or blank."""
    raw = body.model_dump()
    out: Dict[str, Any] = {}
    for key in PLACEMENT_KEYS:
        value = raw.get(key)
        if isinstance(value, str):
            value = value.strip() or None
        if key == "env_vars" and value is not None:
            value = [str(v).strip() for v in value if str(v).strip()] or None
        out[key] = value
    return out


# ── Map suggestions (fixed paths; registered before the legacy catch-all) ────────────

def _suggestion_reason(resource_type: str, recommended_class: str, name: str) -> str:
    reasons = {
        "s3_bucket":             f'S3 bucket "{name}" — a honeytoken object here will alert on any unexpected GetObject access.',
        "iam_user":              f'IAM user "{name}" — a honeytoken access key will immediately signal credential theft if used.',
        "iam_role":              f'IAM role "{name}" — a honeytoken key nearby will detect lateral movement or privilege escalation.',
        "ec2_instance":          f'EC2 instance "{name}" — a fake SSH private key will alert on any unauthorized auth attempt.',
        "lambda_function":       f'Lambda "{name}" is externally reachable — a fake API token here will catch unauthorized access.',
        "elbv2_load_balancer":   f'Load balancer "{name}" is a public entry point — a fake API credential will detect credential stuffing.',
        "secretsmanager_secret": f'Secrets Manager secret "{name}" — a honeytoken credential here will alert on any unauthorized read.',
        "dynamodb_table":        f'DynamoDB table "{name}" — a fake connection string will detect unauthorized access attempts.',
        "ecr_repository":        f'ECR repository "{name}" — a fake registry credential here will catch unauthorized pull attempts.',
        "route53_record":        f'DNS record "{name}" — a honeytoken subdomain will alert on any reconnaissance queries.',
    }
    return reasons.get(resource_type, f'"{name}" ({resource_type}) is a candidate for a honeytoken placement.')


def _suggestion_placement(resource_type: str, resource_id: str, attrs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pre-filled placement for ECS-type suggestions; None otherwise (HP-05 builds the rest)."""
    if resource_type == "ecs_task_definition":
        out: Dict[str, Any] = {"type": "ecs_task_env", "resource_arn": resource_id}
        if attrs.get("service_arn"):
            out["service_arn"] = attrs["service_arn"]
        return out
    if resource_type == "ecs_service":
        out = {"type": "ecs_task_env", "resource_arn": attrs.get("task_definition_arn") or attrs.get("taskDefinition"), "service_arn": resource_id}
        return out
    return None


@router.get("/api/honeypots/suggestions")
def get_honeypot_suggestions(request: Request) -> List[Dict[str, Any]]:
    """Analyse the infra map and suggest honeypot locations."""
    try:
        ctx = resolve_infra_map_context(request)
    except Exception:
        return []

    try:
        graph = ctx["store"].fetch_graph(ctx["default_org"])
    except Exception as exc:
        logger.exception("Failed to load infra map for suggestions")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    suggestions: List[Dict[str, Any]] = []
    seen_ids: set = set()

    for node in graph.get("nodes", []):
        resource_type = getattr(node, "type", None) or ""
        resource_id = getattr(node, "resource_id", None) or ""

        if resource_id in seen_ids:
            continue
        if resource_type not in PUBLIC_RESOURCE_TYPES:
            continue

        recommended_class, priority = PUBLIC_RESOURCE_TYPES[resource_type]
        name = getattr(node, "name", None) or resource_id
        attrs = getattr(node, "attributes", None) or {}
        account_id = getattr(node, "account_id", None) or attrs.get("account_id")
        region = getattr(node, "region", None) or attrs.get("region")

        reason = _suggestion_reason(resource_type, recommended_class, name)

        suggestions.append({
            "resource_id": resource_id,
            "resource_type": resource_type,
            "resource_name": name,
            "account_id": account_id,
            "region": region,
            "recommended_class": recommended_class,
            "priority": priority,
            "reason": reason,
            "placement": _suggestion_placement(resource_type, resource_id, attrs),
            "default_config": {
                "severity": CLASS_DEFAULT_SEVERITY.get(recommended_class, "high"),
                "runbook": CLASS_DEFAULT_RUNBOOKS.get(recommended_class, ""),
            },
        })
        seen_ids.add(resource_id)

    suggestions.sort(key=lambda s: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(s["priority"], 4))
    return suggestions[:50]


# ── Target repo discovery + default selection ──────────────────────────────
# (fixed paths; the legacy catch-all is registered last so they are never captured)

class HoneypotSettingsPayload(BaseModel):
    autopilot: bool = Field(description="Plant every honeytoken the infra map suggests, with default settings")


def _honeypot_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    hp = config.get("honeypot")
    return {"autopilot": bool((hp or {}).get("autopilot", False)) if isinstance(hp, dict) else False}


@router.get("/api/honeypots/settings")
def get_honeypot_settings(request: Request) -> Dict[str, Any]:
    return _honeypot_settings(get_workspace_config(request))


@router.put("/api/honeypots/settings")
def put_honeypot_settings(request: Request, body: HoneypotSettingsPayload) -> Dict[str, Any]:
    """Stores the flag only: nothing plants on its own yet, the suggestions
    panel is still the place where honeytokens get planted."""
    config = get_workspace_config(request)
    hp = config.get("honeypot")
    if not isinstance(hp, dict):
        hp = {}
        config["honeypot"] = hp
    hp["autopilot"] = body.autopilot
    save_config(request, config)
    return _honeypot_settings(config)


class RepoDefaultPayload(BaseModel):
    repo: str = Field(default="", description="owner/name, or empty to clear the default")


@router.get("/api/honeypots/repos")
def list_candidate_repos(request: Request) -> Dict[str, Any]:
    """Repos discovered from the infra map (Terraform root modules), plus the
    currently selected default. The default is where honeypot PRs land when a
    given resource has no owning repo in the map."""
    repos: List[Dict[str, Any]] = []
    try:
        ctx = resolve_infra_map_context(request)
        graph = ctx["store"].fetch_graph(ctx["default_org"])
        repos = [{"repo": r, "module_count": c} for r, c in discover_map_repos(graph)]
    except Exception:
        logger.debug("Repo discovery from map failed", exc_info=True)
        repos = []

    github_cfg = {}
    try:
        github_cfg = get_workspace_config(request).get("github") or {}
    except Exception:
        github_cfg = {}
    default_repo = str(github_cfg.get("honeypot_repo") or "")
    iac_repo = str(github_cfg.get("iac_repo") or "")

    # Effective auto-default when the user hasn't picked one explicitly.
    auto_default = default_repo or iac_repo or (repos[0]["repo"] if repos else "")

    return {
        "repos": repos,
        "default_repo": default_repo,
        "iac_repo": iac_repo,
        "auto_default": auto_default,
    }


@router.post("/api/honeypots/repo-default")
def set_default_repo(request: Request, body: RepoDefaultPayload) -> Dict[str, Any]:
    repo = body.repo.strip()
    if repo and "/" not in repo:
        raise HTTPException(status_code=400, detail="repo must be in owner/name format")

    config = get_workspace_config(request)
    github_cfg = config.setdefault("github", {})
    if not isinstance(github_cfg, dict):
        github_cfg = {}
        config["github"] = github_cfg

    if repo:
        github_cfg["honeypot_repo"] = repo
    else:
        github_cfg.pop("honeypot_repo", None)

    save_config(request, config)
    return {"default_repo": repo}


# ── Honeytoken CRUD ─────────────────────────────────────────────────────────────

@router.get("/api/honeypots/tokens")
def list_honeytokens(request: Request) -> List[Dict[str, Any]]:
    table = _resolve_table(request)
    pk = _ws_pk(request)
    try:
        resp = table.query(
            KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(honeytoken_prefix()),
        )
        items = resp.get("Items", [])
        while resp.get("LastEvaluatedKey"):
            resp = table.query(
                KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(honeytoken_prefix()),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
    except Exception as exc:
        logger.exception("Failed to list honeytokens")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    result = []
    for item in items:
        item = _normalize_item(item)
        honeytoken_id = str(item.get("honeytoken_id", ""))
        count = _count_active_alerts(table, pk, honeytoken_id)
        result.append(_serialize_honeytoken(item, count))
    result.sort(key=lambda a: a.get("created_at", 0), reverse=True)
    return result


@router.post("/api/honeypots/tokens")
def create_honeytoken(request: Request, body: CreateHoneytokenPayload) -> Dict[str, Any]:
    honeytoken_class = normalize_class(body.honeytoken_class)
    if honeytoken_class not in HONEYTOKEN_CLASSES:
        raise HTTPException(status_code=400, detail=f"Invalid honeytoken_class. Must be one of: {', '.join(sorted(HONEYTOKEN_CLASSES))}")

    table = _resolve_table(request)
    pk = _ws_pk(request)
    actor = get_actor_ids(request)
    honeytoken_id = str(uuid.uuid4())
    now = _epoch_ms()

    # If the caller supplied no action_config at all, fall back to class
    # defaults. When a config IS supplied we honor its severity verbatim — the
    # user's explicit choice must not be silently overridden.
    if body.action_config is None:
        action_cfg = ActionConfigPayload(
            severity=CLASS_DEFAULT_SEVERITY.get(honeytoken_class, "high"),
            runbook=CLASS_DEFAULT_RUNBOOKS.get(honeytoken_class, ""),
        )
    else:
        action_cfg = body.action_config
        if not action_cfg.runbook:
            action_cfg.runbook = CLASS_DEFAULT_RUNBOOKS.get(honeytoken_class, "")

    if honeytoken_class == "s3_object" and not (body.resource_path or "").strip("/ "):
        from captains.honeypot.terraform_generator import S3_OBJECT_NEEDS_A_BUCKET

        raise HTTPException(status_code=400, detail=S3_OBJECT_NEEDS_A_BUCKET)

    # An empty name means "pick a realistic cover name for me" (the UI's
    # suggestion flow sends none).
    name = body.name.strip() or default_cover_name(honeytoken_id)
    try:
        material = generate_material(
            honeytoken_class=honeytoken_class,
            name=name,
            region=body.region,
        )
    except ValueError as exc:
        # db_credential is an accepted class with no generator yet.
        raise HTTPException(status_code=501 if "not implemented" in str(exc) else 400, detail=str(exc)) from exc

    item: Dict[str, Any] = {
        "pk": pk,
        "sk": honeytoken_sort_key(honeytoken_id),
        "honeytoken_id": honeytoken_id,
        "honeytoken_class": honeytoken_class,
        "name": name,
        "description": (body.description or "").strip(),
        "status": "pending_pr",
        "created_at": now,
        "updated_at": now,
        "trigger_count": 0,
        "action_config": {
            "severity": action_cfg.severity,
            "runbook": action_cfg.runbook,
            "notification_endpoint_ids": action_cfg.notification_endpoint_ids,
            "auto_block_ip": action_cfg.auto_block_ip,
        },
        "tags": {
            **MANAGED_TAGS,
            "beamreach:honeytoken-id": honeytoken_id,
            "beamreach:honeytoken-class": honeytoken_class,
        },
        "material": material,
    }
    if body.account_id:
        item["account_id"] = body.account_id.strip()
    if body.region:
        item["region"] = body.region.strip()
    if body.resource_path:
        item["resource_path"] = body.resource_path.strip()
    if body.placement is not None:
        item["placement"] = _placement_to_item(body.placement)

    try:
        table.put_item(Item=item)
    except Exception as exc:
        logger.exception("Failed to create honeytoken")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Enqueue captain job to generate the PR
    try:
        job = _queue_client.enqueue(
            captain="honeypot",
            command="plant",
            payload={
                "honeytoken_id": honeytoken_id,
                "honeytoken_class": honeytoken_class,
                "name": name,
                "description": (body.description or "").strip(),
                "account_id": body.account_id,
                "region": body.region,
                "resource_path": body.resource_path,
                "action_config": item["action_config"],
                "placement": item.get("placement"),
                "workspace_id": actor.get("workspace_id"),
                "org_id": actor.get("org_id"),
                "user_id": actor.get("user_id"),
                "tags": item["tags"],
            },
        )
        # Store job_id reference
        table.update_item(
            Key={"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)},
            UpdateExpression="SET plant_job_id = :j",
            ExpressionAttributeValues={":j": str(job.get("job_id", ""))},
        )
    except Exception as exc:
        logger.warning("Failed to enqueue honeypot plant job: %s", exc)

    # Return the material inline on creation so the UI can show the planting
    # instructions immediately (the only time it is returned by the list/detail
    # path is via the dedicated /material endpoint).
    created = _serialize_honeytoken(item, 0)
    created["material"] = _to_native(material)
    return created


@router.get("/api/honeypots/tokens/{honeytoken_id}")
def get_honeytoken(request: Request, honeytoken_id: str) -> Dict[str, Any]:
    table = _resolve_table(request)
    pk = _ws_pk(request)
    try:
        resp = table.get_item(Key={"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    item = resp.get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Honeytoken not found")
    count = _count_active_alerts(table, pk, honeytoken_id)
    return _serialize_honeytoken(item, count)


@router.get("/api/honeypots/tokens/{honeytoken_id}/material")
def get_honeytoken_material(request: Request, honeytoken_id: str) -> Dict[str, Any]:
    """Reveal the material and planting instructions for a honeytoken."""
    table = _resolve_table(request)
    pk = _ws_pk(request)
    resp = table.get_item(Key={"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)})
    item = resp.get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Honeytoken not found")
    item = _normalize_item(item)
    material = _to_native(item.get("material") or {})
    if not material:
        # Older honeytokens created before material was stored — regenerate a
        # fresh one so the UI always has planting guidance.
        try:
            material = generate_material(
                honeytoken_class=str(item.get("honeytoken_class", "")),
                name=str(item.get("name") or default_cover_name(honeytoken_id)),
                region=item.get("region"),
            )
        except ValueError:
            material = {}
    return {"honeytoken_id": honeytoken_id, "honeytoken_class": str(item.get("honeytoken_class", "")), "material": material}


@router.put("/api/honeypots/tokens/{honeytoken_id}")
def update_honeytoken(request: Request, honeytoken_id: str, body: UpdateHoneytokenPayload) -> Dict[str, Any]:
    table = _resolve_table(request)
    pk = _ws_pk(request)

    update_parts = ["updated_at = :now"]
    attrs: Dict[str, Any] = {":now": _epoch_ms()}
    expr_names: Dict[str, str] = {}

    if body.name is not None:
        update_parts.append("name = :name")
        attrs[":name"] = body.name.strip()
    if body.description is not None:
        update_parts.append("description = :desc")
        attrs[":desc"] = body.description.strip()
    if body.account_id is not None:
        update_parts.append("account_id = :acct")
        attrs[":acct"] = body.account_id.strip()
    if body.region is not None:
        update_parts.append("#region = :reg")
        expr_names["#region"] = "region"
        attrs[":reg"] = body.region.strip()
    if body.resource_path is not None:
        update_parts.append("resource_path = :rpath")
        attrs[":rpath"] = body.resource_path.strip()
    if body.status is not None:
        update_parts.append("#st = :status")
        expr_names["#st"] = "status"
        attrs[":status"] = body.status
    if body.action_config is not None:
        update_parts.append("action_config = :ac")
        attrs[":ac"] = {
            "severity": body.action_config.severity,
            "runbook": body.action_config.runbook,
            "notification_endpoint_ids": body.action_config.notification_endpoint_ids,
            "auto_block_ip": body.action_config.auto_block_ip,
        }

    try:
        resp = table.update_item(
            Key={"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)},
            UpdateExpression="SET " + ", ".join(update_parts),
            ExpressionAttributeValues=attrs,
            ExpressionAttributeNames=expr_names if expr_names else None,
            ReturnValues="ALL_NEW",
            ConditionExpression="attribute_exists(pk)",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        raise HTTPException(status_code=404, detail="Honeytoken not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    item = resp.get("Attributes", {})
    count = _count_active_alerts(table, pk, honeytoken_id)
    return _serialize_honeytoken(item, count)


@router.delete("/api/honeypots/tokens/{honeytoken_id}")
def delete_honeytoken(request: Request, honeytoken_id: str) -> Dict[str, Any]:
    table = _resolve_table(request)
    pk = _ws_pk(request)
    actor = get_actor_ids(request)

    # Fetch to get the honeytoken details needed for the decommission PR
    resp = table.get_item(Key={"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)})
    item = resp.get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Honeytoken not found")

    # Mark as decommissioned rather than hard delete — captain handles the PR
    try:
        table.update_item(
            Key={"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)},
            UpdateExpression="SET #st = :s, updated_at = :now",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": "decommissioned", ":now": _epoch_ms()},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Enqueue decommission PR job
    try:
        _queue_client.enqueue(
            captain="honeypot",
            command="decommission",
            payload={
                "honeytoken_id": honeytoken_id,
                "honeytoken_class": str(item.get("honeytoken_class", "")),
                "name": str(item.get("name", "")),
                "account_id": item.get("account_id"),
                "region": item.get("region"),
                "resource_path": item.get("resource_path"),
                "workspace_id": actor.get("workspace_id"),
                "org_id": actor.get("org_id"),
                "user_id": actor.get("user_id"),
            },
        )
    except Exception as exc:
        logger.warning("Failed to enqueue honeypot decommission job: %s", exc)

    return {"status": "decommissioned", "honeytoken_id": honeytoken_id}


# ── Alerts ─────────────────────────────────────────────────────────────────

@router.get("/api/honeypots/tokens/{honeytoken_id}/alerts")
def list_honeytoken_alerts(request: Request, honeytoken_id: str) -> List[Dict[str, Any]]:
    table = _resolve_table(request)
    pk = _ws_pk(request)
    try:
        resp = table.query(
            KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(honeypot_alert_prefix(honeytoken_id)),
        )
        items = resp.get("Items", [])
        while resp.get("LastEvaluatedKey"):
            resp = table.query(
                KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(honeypot_alert_prefix(honeytoken_id)),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    result = [_serialize_alert(i) for i in items]
    result.sort(key=lambda a: a.get("triggered_at", ""), reverse=True)
    return result


@router.post("/api/honeypots/tokens/{honeytoken_id}/alerts/{alert_id}/resolve")
def resolve_alert(request: Request, honeytoken_id: str, alert_id: str) -> Dict[str, Any]:
    table = _resolve_table(request)
    pk = _ws_pk(request)
    now = datetime.now(timezone.utc).isoformat()
    try:
        table.update_item(
            Key={"pk": pk, "sk": honeypot_alert_sort_key(honeytoken_id, alert_id)},
            UpdateExpression="SET resolved_at = :r",
            ExpressionAttributeValues={":r": now},
            ConditionExpression="attribute_exists(pk)",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        raise HTTPException(status_code=404, detail="Alert not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "resolved", "alert_id": alert_id, "resolved_at": now}


# ── Execute runbook (dispatch notifications) ───────────────────────────────

@router.post("/api/honeypots/tokens/{honeytoken_id}/execute-runbook")
def execute_runbook(request: Request, honeytoken_id: str, body: ExecuteRunbookPayload) -> Dict[str, Any]:
    table = _resolve_table(request)
    pk = _ws_pk(request)
    actor = get_actor_ids(request)

    resp = table.get_item(Key={"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)})
    if not resp.get("Item"):
        raise HTTPException(status_code=404, detail="Honeytoken not found")

    try:
        _queue_client.enqueue(
            captain="honeypot",
            command="dispatch_runbook",
            payload={
                "honeytoken_id": honeytoken_id,
                "runbook": body.runbook,
                "notification_endpoint_ids": body.notification_endpoint_ids,
                "workspace_id": actor.get("workspace_id"),
                "org_id": actor.get("org_id"),
                "user_id": actor.get("user_id"),
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"status": "dispatched", "honeytoken_id": honeytoken_id}


# ── Placement (where the honeytoken is; exactly one per token) ──────────────────

@router.put("/api/honeypots/tokens/{honeytoken_id}/placement")
def set_placement(request: Request, honeytoken_id: str, body: PlacementPayload) -> Dict[str, Any]:
    """Replace the token's placement. Never appends: a token is in one place."""
    return _write_placement(request, honeytoken_id, _placement_to_item(body))


@router.delete("/api/honeypots/tokens/{honeytoken_id}/placement")
def clear_placement(request: Request, honeytoken_id: str) -> Dict[str, Any]:
    return _write_placement(request, honeytoken_id, None)


def _write_placement(request: Request, honeytoken_id: str, placement: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    table = _resolve_table(request)
    pk = _ws_pk(request)
    try:
        resp = table.update_item(
            Key={"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)},
            UpdateExpression="SET placement = :p, updated_at = :now",
            ExpressionAttributeValues={":p": placement, ":now": _epoch_ms()},
            ReturnValues="ALL_NEW",
            ConditionExpression="attribute_exists(pk)",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        raise HTTPException(status_code=404, detail="Honeytoken not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    count = _count_active_alerts(table, pk, honeytoken_id)
    return _serialize_honeytoken(resp.get("Attributes", {}), count)


_TEST_FIRE_TIMEOUT_S = 30.0


def _real_test_fire(table, pk: str, honeytoken_id: str, honeytoken: Dict[str, Any]) -> Dict[str, Any]:
    """Call STS with the honeytoken key from Compass and wait for the alert that
    CloudTrail -> EventBridge -> SQS produces, then mark it as a test."""
    from captains.honeypot import arm

    material = _to_native(honeytoken.get("material") or {})
    key_id, secret = material.get("access_key_id"), material.get("secret_access_key")
    if not (key_id and secret):
        raise HTTPException(
            status_code=409,
            detail="Test-fire needs the key id (filled by /arm) and the secret stored in the token material.",
        )
    region = str(honeytoken.get("region") or material.get("region") or os.getenv("AWS_REGION") or "us-east-1")
    since = arm.now_iso()
    started = time.monotonic()
    try:
        arm.fire_aws_credential(str(key_id), str(secret), region)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Test call with the honeytoken key failed: {exc}") from exc
    alert = arm.wait_for_test_alert(table, pk, honeytoken_id, since, timeout_s=_TEST_FIRE_TIMEOUT_S)
    if not alert:
        return {"status": "not_delivered", "is_test": True, "mode": "real", "delivered": False,
                "waited_ms": int((time.monotonic() - started) * 1000),
                "note": f"No alert arrived within {int(_TEST_FIRE_TIMEOUT_S)} s (CloudTrail delivery is best effort)."}
    round_trip_ms = int((time.monotonic() - started) * 1000)
    try:
        table.update_item(
            Key={"pk": pk, "sk": alert["sk"]},
            UpdateExpression="SET is_test = :t, round_trip_ms = :r",
            ExpressionAttributeValues={":t": True, ":r": round_trip_ms},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "fired", "alert_id": alert.get("alert_id"), "is_test": True, "mode": "real",
            "delivered": True, "round_trip_ms": round_trip_ms}


# ── Test fire (prove a honeytoken is armed end-to-end) ─────────────────────────

@router.post("/api/honeypots/tokens/{honeytoken_id}/test-fire")
def test_fire(request: Request, honeytoken_id: str) -> Dict[str, Any]:
    """Synthetically trigger this honeytoken through the real alert path so the
    user can confirm detection + notification work without waiting for a real
    intrusion. The resulting alert is flagged as a test. Not in demo mode, where
    "Trip it" (below) is the one way to fire a decoy."""
    from compass_core import demo as demo_mode

    if demo_mode.enabled():
        raise HTTPException(status_code=404, detail="Not in the demo: trip a pre-armed decoy instead")
    table = _resolve_table(request)
    pk = _ws_pk(request)
    actor = get_actor_ids(request)

    honeytoken = _normalize_item(table.get_item(Key={"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)}).get("Item") or {})
    if not honeytoken:
        raise HTTPException(status_code=404, detail="Honeytoken not found")

    if normalize_class(honeytoken.get("honeytoken_class")) == "aws_credential":
        return _real_test_fire(table, pk, honeytoken_id, honeytoken)

    alert_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    alert_item = {
        "pk": pk,
        "sk": honeypot_alert_sort_key(honeytoken_id, alert_id),
        "alert_id": alert_id,
        "honeytoken_id": honeytoken_id,
        "triggered_at": now_iso,
        "is_test": True,
        "source_ip": "203.0.113.7",
        "ip_type": "external",
        "service": "test-fire",
    }
    try:
        table.put_item(Item=alert_item)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    action_config = _to_native(honeytoken.get("action_config") or {})
    placement_note = _placement_summary(_get_placement(table, pk, honeytoken_id))
    try:
        _queue_client.enqueue(
            captain="honeypot",
            command="alert_notify",
            payload={
                "alert_id": alert_id,
                "honeytoken_id": honeytoken_id,
                "honeytoken_name": str(honeytoken.get("name", "")),
                "honeytoken_class": str(honeytoken.get("honeytoken_class", "")),
                "source_ip": alert_item["source_ip"],
                "ip_type": "external",
                "triggered_at": now_iso,
                "action_config": action_config,
                "placement_note": placement_note,
                "is_test": True,
                "workspace_id": actor.get("workspace_id"),
            },
        )
    except Exception as exc:
        logger.warning("Failed to enqueue test-fire notification: %s", exc)

    # A decoy database listener is private to the customer VPC, so Compass cannot
    # connect to it: this class exercises storage and notification only.
    return {
        "status": "fired", "alert_id": alert_id, "is_test": True, "mode": "synthetic",
        "note": "Synthetic alert: detection in the target account was not exercised for this class.",
    }


# ── Trip it (demo mode): a visitor fires a pre-armed decoy for real, paced ───────────

@router.post("/api/honeypots/tokens/{honeytoken_id}/trip")
def trip_honeytoken(request: Request, honeytoken_id: str) -> Dict[str, Any]:
    """Demo mode only (404 otherwise). The decoy must be one of ``demo.trippable`` and armed.
    One trip per decoy per cooldown, shared by every visitor: during it the answer is the trip
    in force (``state: cooldown``) with the incident it opened, so the visitor is taken there.
    A visitor gets ``trips_per_visitor_per_hour``. The fire itself is the ``honeypot trip`` job."""
    from compass_core import demo as demo_mode

    if not demo_mode.enabled():
        raise HTTPException(status_code=404, detail="Not a demo")
    table = _resolve_table(request)
    pk = _ws_pk(request)
    actor = get_actor_ids(request)
    settings = demo_mode.settings(get_workspace_config(request))
    honeytoken = _normalize_item(table.get_item(Key={"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)}).get("Item") or {})
    if not honeytoken:
        raise HTTPException(status_code=404, detail="Honeytoken not found")
    if honeytoken_id not in set(settings["trippable"]):
        raise HTTPException(status_code=409, detail="Only the pre-armed decoys can be tripped; this one is a plant that was never applied")
    if honeytoken.get("status") != "armed":
        raise HTTPException(status_code=409, detail=f"The decoy is {honeytoken.get('status') or 'not armed'}: it re-arms after the response, try again in a minute")
    material = _to_native(honeytoken.get("material") or {})
    if not (material.get("access_key_id") and material.get("secret_access_key")):
        raise HTTPException(status_code=409, detail="The decoy's key is not stored with Compass: arm it again with its secret")

    now = demo_mode.epoch_ms()
    me = str(actor.get("user_id") or "anonymous")
    current = demo_mode.cooldown_row(table, pk, honeytoken_id)
    if demo_mode.in_force(current, now_ms=now):
        return {"state": "cooldown", **demo_mode.public_cooldown(current, me=me, now_ms=now)}
    if len(demo_mode.visitor_trips(table, pk, me, now_ms=now)) >= int(settings["trips_per_visitor_per_hour"]):
        raise HTTPException(status_code=429, detail=f"{settings['trips_per_visitor_per_hour']} trips an hour per visitor: the open incidents below are the same story")
    claim = demo_mode.claim_trip(table, pk, honeytoken_id, user_id=me, cooldown_seconds=int(settings["cooldown_seconds"]), now_ms=now)
    if not claim["claimed"]:
        return {"state": "cooldown", **demo_mode.public_cooldown(claim["row"], me=me, now_ms=now)}
    demo_mode.record_visitor_trip(table, pk, me, now_ms=now)
    job = _queue_client.enqueue(
        captain="honeypot", command="trip",
        payload={"honeytoken_id": honeytoken_id, "workspace_id": actor.get("workspace_id"), "org_id": actor.get("org_id"), "user_id": actor.get("user_id")},
    )
    demo_mode.record_trip(table, pk, honeytoken_id, job_id=job.get("job_id"))
    return {"state": "tripped", **demo_mode.public_cooldown({**claim["row"], "job_id": job.get("job_id")}, me=me, now_ms=now)}


# ── Arm (mark honeytoken verified/active once the PR is applied) ─────────────────

class ArmPayload(BaseModel):
    # the decoy's secret, from the apply output: stored so Compass can use the key itself (test-fire, the demo's trip)
    secret_access_key: Optional[str] = None


@router.post("/api/honeypots/tokens/{honeytoken_id}/arm")
def arm_honeytoken(request: Request, honeytoken_id: str, body: ArmPayload = ArmPayload()) -> Dict[str, Any]:
    """Verify the planted resources in the target account (read-only), then mark
    the token armed. Refuses with the failed checks when anything is missing,
    notably when no CloudTrail trail is logging."""
    from captains.honeypot import arm

    table = _resolve_table(request)
    pk = _ws_pk(request)
    key = {"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)}
    item = table.get_item(Key=key).get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Honeytoken not found")
    token = _normalize_item(item)
    placement = _to_native(token.get("placement")) or None
    try:
        # Verify where the honeytoken lives: the placement's ARNs name the
        # region; the workspace default may be another region entirely.
        from captains.honeypot.investigate import region_of_arns

        region = token.get("region") or region_of_arns(
            (placement or {}).get("service_arn"), (placement or {}).get("cluster_arn"), (placement or {}).get("resource_arn")
        )
        session = sessions.target("read", region=region, config=get_workspace_config(request))
        if session is not None and region and session.region_name != region:
            # the workspace's configured region wins in target(); pin the session to the placement's region
            session = sessions.with_region(session, region)
        result = arm.verify_honeytoken(session, _to_native(token), placement, config=get_workspace_config(request))
    except Exception as exc:
        result = {"ok": False, "checks": [{"name": "target_account_access", "ok": False, "detail": str(exc)[:300]}]}
    if not result["ok"]:
        raise HTTPException(status_code=409, detail={"message": "Honeytoken is not ready to arm", **result})

    names = {"#st": "status"}
    values: Dict[str, Any] = {":s": "armed", ":now": _epoch_ms()}
    expr = "SET #st = :s, updated_at = :now, armed_at = :now"
    if result.get("access_key_id") and isinstance(item.get("material"), dict):
        expr += ", material.access_key_id = :k, material.#ms = :ms"
        names["#ms"] = "status"
        values[":k"] = result["access_key_id"]
        values[":ms"] = "applied"
    if body.secret_access_key and isinstance(item.get("material"), dict):
        expr += ", material.secret_access_key = :sec"
        values[":sec"] = body.secret_access_key
    try:
        resp = table.update_item(
            Key=key, UpdateExpression=expr, ExpressionAttributeNames=names,
            ExpressionAttributeValues=values, ReturnValues="ALL_NEW",
            ConditionExpression="attribute_exists(pk)",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        raise HTTPException(status_code=404, detail="Honeytoken not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    count = _count_active_alerts(table, pk, honeytoken_id)
    out = _serialize_honeytoken(resp.get("Attributes", {}), count)
    out["checks"] = result["checks"]
    return out


# ── Inbound alert webhook (called by the API-token receiver Lambda) ───────────────

class InboundAlertPayload(BaseModel):
    # Generated resources send `token_id` (no product word in anything AWS
    # shows); plants deployed before the rename still send `asset_id`.
    honeytoken_id: Optional[str] = None
    token_id: Optional[str] = None
    asset_id: Optional[str] = None
    workspace_id: str
    source_ip: Optional[str] = None
    ip_type: Optional[str] = None
    principal_arn: Optional[str] = None
    user_agent: Optional[str] = None
    service: Optional[str] = None
    event_time: Optional[str] = None
    event_id: Optional[str] = None
    event_name: Optional[str] = None
    region: Optional[str] = None
    access_key_id: Optional[str] = None
    error_code: Optional[str] = None
    vpc_endpoint_id: Optional[str] = None
    raw_event: Optional[Dict[str, Any]] = None


@router.post("/api/honeypots/inbound-alert")
def ingest_alert(request: Request, body: InboundAlertPayload) -> Dict[str, Any]:
    """Record a honeytoken trip submitted over HTTP.

    **This is not the production delivery path.** Honeytoken trips arrive over SQS:
    an EventBridge rule in the target account delivers cross-account to a queue
    the control plane owns, authenticated by IAM and reachable from nowhere
    else. See captains/honeypot/alert_consumer.py.

    This endpoint is retained for testing and for callback-style honeytokens, and
    it takes `request` so it sits behind the same session gate as every other
    route. It previously took only `body`, which meant no actor was ever
    resolved and the route was writable by anyone on the internet who knew a
    workspace id — enough to fabricate breach alerts, page an on-call, and bury
    a genuine trip under noise.
    """
    _ = get_actor_ids(request)  # enforces authentication; raises if unauthenticated

    payload = body.model_dump()
    payload["honeytoken_id"] = body.honeytoken_id or body.asset_id
    payload.pop("asset_id", None)
    if not payload["honeytoken_id"]:
        raise HTTPException(status_code=422, detail="honeytoken_id is required")

    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    from compass_api import workspace as api_workspace

    try:
        ctx = api_workspace.load(body.workspace_id)
        dynamodb_config = ctx.config.get("dynamodb_config") or {}
        table_name = tables(dynamodb_config).items
        if not table_name:
            raise HTTPException(status_code=500, detail="Table not configured for workspace")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    table = sessions.control(region=region).resource("dynamodb").Table(table_name)

    try:
        item = record_alert(
            table,
            workspace_pk(body.workspace_id),
            payload,
            workspace_id=body.workspace_id,
            queue_client=_queue_client,
            placement_summary=_placement_summary,
            get_placement=_get_placement,
            to_native=_to_native,
        )
    except Exception as exc:
        logger.exception("Failed to store inbound alert")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"status": "recorded", "alert_id": item["alert_id"]}


# ── Autopilot ──────────────────────────────────────────────────────────────

@router.post("/api/honeypots/autopilot")
def autopilot_plant(request: Request, body: AutopilotPayload) -> Dict[str, Any]:
    """Create honeytokens for all given suggestions with default settings."""
    if not body.suggestions:
        raise HTTPException(status_code=400, detail="No suggestions provided")

    created = 0
    for suggestion in body.suggestions:
        if normalize_class(suggestion.recommended_class) not in HONEYTOKEN_CLASSES:
            continue
        default_cfg = suggestion.default_config or {}
        action_payload = ActionConfigPayload(
            severity=default_cfg.get("severity") or CLASS_DEFAULT_SEVERITY.get(suggestion.recommended_class, "high"),
            runbook=default_cfg.get("runbook") or CLASS_DEFAULT_RUNBOOKS.get(suggestion.recommended_class, ""),
            notification_endpoint_ids=body.notification_endpoint_ids,
            auto_block_ip=False,
        )
        token_payload = CreateHoneytokenPayload(
            honeytoken_class=suggestion.recommended_class,
            name=f"{default_cover_name(suggestion.resource_id)}-{hashlib.sha1(suggestion.resource_id.encode()).hexdigest()[:6]}",
            description=suggestion.description or f"Auto-planted by Autopilot for {suggestion.resource_id}",
            account_id=suggestion.account_id,
            region=suggestion.region,
            resource_path=suggestion.resource_id,
            action_config=action_payload,
        )
        try:
            create_honeytoken(request, token_payload)
            created += 1
        except Exception as exc:
            logger.warning("Autopilot failed to plant honeytoken for %s: %s", suggestion.resource_id, exc)

    return {"status": "ok", "created": created, "requested": len(body.suggestions)}


# ── Legacy paths (308 → /api/honeypots/tokens/…) ───────────────────────────
#
# Registered after every other route on purpose: a catch-all declared earlier
# would capture the fixed paths ("suggestions", "repos", …). Starlette matches
# in registration order, so these only see what nothing above claimed.

_FIXED_SEGMENTS = {"suggestions", "repos", "repo-default", "inbound-alert", "autopilot", "settings", "tokens", "actions", "audits", "incidents"}
_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def _redirect_to_tokens(request: Request, tail: str = "") -> RedirectResponse:
    target = "/api/honeypots/tokens" + (f"/{tail}" if tail else "")
    if request.url.query:
        target += f"?{request.url.query}"
    return RedirectResponse(url=target, status_code=308)


@router.api_route("/api/honeypots", methods=["GET", "POST"], include_in_schema=False)
def legacy_collection(request: Request):
    return _redirect_to_tokens(request)


@router.api_route("/api/honeypots/{legacy_id}", methods=_ALL_METHODS, include_in_schema=False)
def legacy_item(request: Request, legacy_id: str):
    if legacy_id in _FIXED_SEGMENTS:
        # A real route owns this path but not this method.
        raise HTTPException(status_code=405, detail="Method not allowed")
    return _redirect_to_tokens(request, legacy_id)


@router.api_route("/api/honeypots/{legacy_id}/{rest:path}", methods=_ALL_METHODS, include_in_schema=False)
def legacy_item_subpath(request: Request, legacy_id: str, rest: str):
    if legacy_id in _FIXED_SEGMENTS:
        raise HTTPException(status_code=404, detail="Not found")
    if rest == "lure":
        rest = "material"
    return _redirect_to_tokens(request, f"{legacy_id}/{rest}")
