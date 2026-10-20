"""The decoys' commands, for the open dispatcher (compass_core.dispatch)."""

from __future__ import annotations

from typing import Any, Dict

from compass_core import dispatch
from compass_core.workspace import captain_config, resolve


def _captain(payload: Dict[str, Any]):
    from captains.honeypot.captain import HoneypotCaptain

    workspace = resolve(payload)
    return HoneypotCaptain(config=captain_config(payload, workspace), workspace_id=workspace.workspace_id)


def plant(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _captain(payload).run_plant(honeytoken=payload)


def decommission(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _captain(payload).run_decommission(honeytoken=payload)


def alert_notify(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _captain(payload).run_alert_notify(payload=payload)


def dispatch_runbook(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _captain(payload).run_dispatch_runbook(payload=payload)


def investigate(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _captain(payload).run_investigate(payload=payload)


def respond(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _captain(payload).run_respond(payload=payload)


def responder_pr(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _captain(payload).run_responder_pr(payload=payload)


def create(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Create a honeytoken record and plant it (``plant=false`` to only create).

    payload: honeytoken_class (required), name, description, region, account_id,
    resource_path, placement {type, ...}, action_config {severity, runbook, ...}.
    What the product's API does on POST /api/honeypots, from the shell."""
    from captains.honeypot.catalog import new_honeytoken_item, plant_payload
    from compass_core.queue import job_queue
    from compass_core.tables import dynamo_table, tables

    workspace = resolve(payload)
    config = captain_config(payload, workspace)
    table_name = tables(config).items
    if not table_name:
        raise RuntimeError("dynamodb_config.items is not configured")
    item = new_honeytoken_item(
        workspace_id=workspace.workspace_id,
        honeytoken_class=str(payload.get("honeytoken_class") or payload.get("class") or ""),
        name=str(payload.get("name") or ""),
        description=str(payload.get("description") or ""),
        region=payload.get("region"),
        account_id=payload.get("account_id"),
        resource_path=payload.get("resource_path"),
        placement=payload.get("placement") if isinstance(payload.get("placement"), dict) else None,
        action_cfg=payload.get("action_config") if isinstance(payload.get("action_config"), dict) else None,
    )
    dynamo_table(table_name).put_item(Item=item)
    result: Dict[str, Any] = {
        "honeytoken_id": item["honeytoken_id"],
        "name": item["name"],
        "honeytoken_class": item["honeytoken_class"],
        "status": item["status"],
        "material": item["material"],
    }
    if payload.get("plant", True) in (False, "false", "0", 0):
        return result
    job = job_queue().enqueue(
        captain="honeypot",
        command="plant",
        payload=plant_payload(item, workspace_id=workspace.workspace_id, org_id=workspace.org_id, user_id=workspace.user_id),
    )
    result["plant_job"] = {k: job.get(k) for k in ("job_id", "status", "result_summary") if k in job}
    return result


def incidents(payload: Dict[str, Any]) -> Dict[str, Any]:
    """List incidents, newest first (limit=50); with incident_id, that one with its alerts and audit trail."""
    from captains.honeypot import incidents as store
    from compass_core.keys import workspace_pk
    from compass_core.tables import dynamo_table, tables

    workspace = resolve(payload)
    table = dynamo_table(tables(captain_config(payload, workspace)).items)
    pk = workspace_pk(workspace.workspace_id)
    incident_id = payload.get("incident_id")
    if incident_id:
        return {
            "incident": store.get_incident(table, pk, str(incident_id)),
            "alerts": store.alerts_for_incident(table, pk, str(incident_id)),
            "audit": store.audits_for_incident(table, pk, str(incident_id)),
        }
    return {"incidents": store.list_incidents(table, pk, limit=int(payload.get("limit") or 50))}


def endpoint_add(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Add a notification endpoint: type=webhook|slack|smtp with name and its fields
    (webhook_url; or host/port/username/password/recipients for smtp)."""
    import uuid

    from captains.honeypot.catalog import epoch_ms
    from compass_core.keys import notification_endpoint_sort_key, workspace_pk
    from compass_core.tables import dynamo_table, tables

    kind = str(payload.get("type") or "")
    if kind not in ("webhook", "slack", "smtp", "email"):
        raise ValueError("type must be webhook, slack, smtp or email")
    workspace = resolve(payload)
    endpoint_id = str(uuid.uuid4())
    item: Dict[str, Any] = {
        "pk": workspace_pk(workspace.workspace_id),
        "sk": notification_endpoint_sort_key(endpoint_id),
        "endpoint_id": endpoint_id,
        "name": str(payload.get("name") or kind),
        "type": kind,
        "enabled": True,
        "created_at": epoch_ms(),
    }
    for key in ("webhook_url", "channel", "recipients", "host", "port", "username", "password", "use_tls", "use_ssl", "bot_token"):
        if payload.get(key) not in (None, ""):
            item[key] = payload[key]
    dynamo_table(tables(captain_config(payload, workspace)).items).put_item(Item=item)
    return {"endpoint_id": endpoint_id, "type": kind, "name": item["name"]}


def action_set(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Enable a response action: action_type=iam_deactivate_key|ecs_restart_task
    mode=off|approve|autopilot (default approve) risk_max=1..5 (default 2),
    optional scope {service_arns, user_arns, user_path_prefixes}."""
    import uuid

    from captains.honeypot.actions import ACTION_CATALOG
    from captains.honeypot.catalog import epoch_ms
    from compass_core.keys import honeypot_action_sort_key, workspace_pk
    from compass_core.policy import MODES
    from compass_core.tables import dynamo_table, tables

    action_type = str(payload.get("action_type") or "")
    if action_type not in ACTION_CATALOG:
        raise ValueError(f"action_type must be one of {', '.join(sorted(ACTION_CATALOG))}")
    mode = str(payload.get("mode") or "approve")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    risk_max = int(payload.get("risk_max") or 2)
    if not 1 <= risk_max <= 5:
        raise ValueError("risk_max must be between 1 and 5")
    scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
    workspace = resolve(payload)
    action_id = f"act-{uuid.uuid4().hex[:12]}"
    item = {
        "pk": workspace_pk(workspace.workspace_id),
        "sk": honeypot_action_sort_key(action_id),
        "action_id": action_id,
        "action_type": action_type,
        "scope": {k: list(scope.get(k) or []) for k in ("service_arns", "user_arns", "user_path_prefixes")},
        "mode": mode,
        "risk_max": risk_max,
        "enabled": payload.get("enabled", True) not in (False, "false", "0", 0),
        "created_at": epoch_ms(),
    }
    dynamo_table(tables(captain_config(payload, workspace)).items).put_item(Item=item)
    return {k: item[k] for k in ("action_id", "action_type", "mode", "risk_max", "enabled")}


def _token_item(payload: Dict[str, Any]):
    from compass_core.keys import honeytoken_sort_key, workspace_pk
    from compass_core.tables import dynamo_table, tables

    workspace = resolve(payload)
    config = captain_config(payload, workspace)
    table = dynamo_table(tables(config).items)
    honeytoken_id = str(payload.get("honeytoken_id") or "")
    if not honeytoken_id:
        raise ValueError("honeytoken_id is required")
    key = {"pk": workspace_pk(workspace.workspace_id), "sk": honeytoken_sort_key(honeytoken_id)}
    item = table.get_item(Key=key).get("Item")
    if not item:
        raise LookupError(f"honeytoken {honeytoken_id} not found in workspace {workspace.workspace_id}")
    return workspace, config, table, key, item


def arm(payload: Dict[str, Any]) -> Dict[str, Any]:
    """After the decoy PR is applied: verify the planted resources (read-only) and mark the token armed.
    Stores the decoy's access key id on the record; the secret stays with you (apply output)."""
    from captains.honeypot import arm as arming
    from captains.honeypot.catalog import epoch_ms
    from captains.honeypot.placement import to_native
    from compass_core import sessions

    workspace, config, table, key, item = _token_item(payload)
    token = to_native(item)
    placement = token.get("placement") or None
    region = str(payload.get("region") or token.get("region") or (config.get("aws") or {}).get("region") or config.get("aws_region") or "us-east-1")
    session = sessions.target("read", region=region, config=config)
    result = arming.verify_honeytoken(session, token, placement)
    if not result.get("ok"):
        return {"status": "not_ready", "honeytoken_id": token["honeytoken_id"], **result}
    names = {"#st": "status"}
    values: Dict[str, Any] = {":s": "armed", ":now": epoch_ms()}
    expr = "SET #st = :s, updated_at = :now, armed_at = :now"
    if result.get("access_key_id") and isinstance(item.get("material"), dict):
        expr += ", material.access_key_id = :k, material.#ms = :ms"
        names["#ms"] = "status"
        values[":k"] = result["access_key_id"]
        values[":ms"] = "applied"
    table.update_item(Key=key, UpdateExpression=expr, ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    return {"status": "armed", "honeytoken_id": token["honeytoken_id"], "access_key_id": result.get("access_key_id"), "checks": result.get("checks")}


def fire(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Test-fire an aws_credential decoy: one STS call with its key, then wait for the alert
    `compass serve` records (timeout=30 s). Needs secret_access_key=… from the apply output
    unless it is already on the record."""
    import time

    from captains.honeypot import arm as arming
    from captains.honeypot.placement import to_native
    from compass_core.keys import workspace_pk

    workspace, config, table, key, item = _token_item(payload)
    token = to_native(item)
    material = token.get("material") or {}
    key_id = payload.get("access_key_id") or material.get("access_key_id")
    secret = payload.get("secret_access_key") or material.get("secret_access_key")
    if not (key_id and secret):
        raise ValueError("the decoy's access_key_id (run `honeypot arm` first) and secret_access_key (from the apply output) are needed")
    region = str(payload.get("region") or token.get("region") or material.get("region") or "us-east-1")
    since = arming.now_iso()
    started = time.monotonic()
    arming.fire_aws_credential(str(key_id), str(secret), region)
    timeout = float(payload.get("timeout") or 30)
    alert = arming.wait_for_test_alert(table, workspace_pk(workspace.workspace_id), token["honeytoken_id"], since, timeout_s=timeout)
    out: Dict[str, Any] = {"honeytoken_id": token["honeytoken_id"], "fired_at": since, "waited_ms": int((time.monotonic() - started) * 1000)}
    if alert:
        out.update(status="delivered", alert_id=alert.get("alert_id"), incident_id=alert.get("incident_id"))
    else:
        out.update(status="not_delivered", note=f"no alert arrived within {int(timeout)} s: is `compass serve` running, and is CloudTrail logging management events? Delivery is best effort and can take longer.")
    return out


COMMANDS = {
    "create": create,
    "incidents": incidents,
    "endpoint_add": endpoint_add,
    "action_set": action_set,
    "arm": arm,
    "fire": fire,
    "plant": plant,
    "decommission": decommission,
    "alert_notify": alert_notify,
    "dispatch_runbook": dispatch_runbook,
    "investigate": investigate,
    "respond": respond,
    "responder_pr": responder_pr,
}

dispatch.register("honeypot", COMMANDS)
