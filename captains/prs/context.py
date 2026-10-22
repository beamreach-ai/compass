"""What the originating captain knew, loaded fresh before a revision.

One provider per origin. Each returns the same shape and never raises: a
missing table or item becomes a note, not a failure, so a revision can still
run on the intent alone.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, Optional

from compass_core.keys import (
    audit_finding_sort_key,
    honeytoken_sort_key,
    progress_session_prefix,
    rope_sort_key,
    workspace_pk,
)
from compass_core.tables import dynamo_table, tables

logger = logging.getLogger("prs.context")

_TEXT_CAP = 1500
_LIST_CAP = 20


def _native(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_native(v) for v in value]
    return value


def _cap(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _TEXT_CAP:
        return value[: _TEXT_CAP - 3] + "..."
    if isinstance(value, list):
        return [_cap(v) for v in value[:_LIST_CAP]]
    if isinstance(value, dict):
        return {k: _cap(v) for k, v in value.items()}
    return value


def _empty() -> Dict[str, Any]:
    return {"summary": None, "entities": [], "evidence": [], "history": [], "changed_since_pr": [], "notes": []}


def _table(name: Optional[str]):
    if not name:
        return None
    return dynamo_table(name)


def _dynamodb_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("dynamodb_config") if isinstance(config.get("dynamodb_config"), dict) else {}
    out = dict(raw)
    if out.get("audit") and "items" not in out:
        out["items"] = out["audit"]
    return out


# ── providers ──────────────────────────────────────────────────────────────

def _finding_context(record: Dict[str, Any], *, config: Dict[str, Any], workspace_id: str) -> Dict[str, Any]:
    out = _empty()
    finding_id = (record.get("origin") or {}).get("finding_id")
    if not finding_id:
        out["notes"].append("origin has no finding id")
        return out
    ddb = _dynamodb_config(config)
    table = _table(tables(ddb).items)
    if table is None:
        out["notes"].append("findings table not configured")
        return out
    pk = workspace_pk(workspace_id)
    try:
        item = _native(table.get_item(Key={"pk": pk, "sk": audit_finding_sort_key(str(finding_id))}).get("Item") or {})
    except Exception as exc:
        out["notes"].append(f"finding lookup failed: {exc}")
        return out
    if not item:
        out["notes"].append(f"finding {finding_id} not found")
        return out
    triage = item.get("triage") if isinstance(item.get("triage"), dict) else {}
    finding = {
        "kind": "finding",
        "id": item.get("id") or finding_id,
        "title": item.get("title") or item.get("check") or item.get("check_id") or item.get("name"),
        "severity": item.get("severity"),
        "status": item.get("status"),
        "resource": item.get("resource_id") or item.get("resource_arn") or item.get("resource"),
        "resource_type": item.get("resource_type"),
        "tool": item.get("tool"),
        "description": _cap(item.get("description") or item.get("details") or item.get("remediation")),
        "triage": _cap({k: triage.get(k) for k in ("risk", "urgency", "summary", "reasoning", "rationale", "recommended_action", "change_type") if k in triage}),
    }
    out["entities"].append(finding)
    for key in ("evidence", "details", "metadata"):
        if item.get(key):
            out["evidence"].append({"source": key, "value": _cap(item.get(key))})
    out["summary"] = (triage.get("summary") if isinstance(triage.get("summary"), str) else None) or finding["title"]

    progress = _table(tables(ddb).progress)
    if progress is not None:
        try:
            from boto3.dynamodb.conditions import Key

            resp = progress.query(
                KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(progress_session_prefix(str(finding_id))),
                ScanIndexForward=False,
                Limit=_LIST_CAP,
            )
            rows = [_native(r) for r in resp.get("Items", [])]
            rows.reverse()
            out["history"] = [{"at": r.get("timestamp") or r.get("ts"), "phase": r.get("phase"), "message": _cap(r.get("message"))} for r in rows]
        except Exception as exc:
            out["notes"].append(f"progress lookup failed: {exc}")

    status = str(item.get("status") or "").lower()
    if status in ("pass", "resolved", "completed", "fixed"):
        out["changed_since_pr"].append(f"finding is now {status}: the control may already be in place")
    intent_risk = (record.get("intent") or {}).get("risk")
    if isinstance(triage.get("risk"), (int, float)) and intent_risk is not None and int(triage["risk"]) != int(intent_risk):
        out["changed_since_pr"].append(f"triage risk changed from {intent_risk} to {int(triage['risk'])}")
    return out


def _rope_context(record: Dict[str, Any], *, config: Dict[str, Any], workspace_id: str) -> Dict[str, Any]:
    out = _empty()
    rope_id = (record.get("origin") or {}).get("rope_id")
    if not rope_id:
        return out
    table = _table(tables(_dynamodb_config(config)).ropes)
    if table is None:
        out["notes"].append("ropes table not configured")
        return out
    try:
        item = _native(table.get_item(Key={"pk": workspace_pk(workspace_id), "sk": rope_sort_key(str(rope_id))}).get("Item") or {})
    except Exception as exc:
        out["notes"].append(f"rope lookup failed: {exc}")
        return out
    if not item:
        out["notes"].append(f"request {rope_id} not found")
        return out
    out["entities"].append({
        "kind": "request",
        "id": rope_id,
        "message": _cap(item.get("message")),
        "target": item.get("target"),
        "status": item.get("status"),
        "metadata": _cap(item.get("metadata")),
        "triage": _cap(item.get("triage")),
    })
    logs = item.get("logs") if isinstance(item.get("logs"), list) else []
    out["history"] = [
        {"at": l.get("ts"), "phase": l.get("phase"), "message": _cap(l.get("message"))} if isinstance(l, dict) else {"message": _cap(str(l))}
        for l in logs[-_LIST_CAP:]
    ]
    out["summary"] = _cap(item.get("message"))
    return out


def _honeypot_context(record: Dict[str, Any], *, config: Dict[str, Any], workspace_id: str) -> Dict[str, Any]:
    out = _empty()
    token_id = (record.get("origin") or {}).get("honeytoken_id")
    if not token_id:
        out["notes"].append("origin has no honeytoken id")
        return out
    table = _table(tables(_dynamodb_config(config)).items)
    if table is None:
        out["notes"].append("items table not configured")
        return out
    pk = workspace_pk(workspace_id)
    try:
        item = _native(table.get_item(Key={"pk": pk, "sk": honeytoken_sort_key(str(token_id))}).get("Item") or {})
    except Exception as exc:
        out["notes"].append(f"honeytoken lookup failed: {exc}")
        return out
    if not item:
        out["notes"].append(f"honeytoken {token_id} not found")
        return out
    item.pop("material", None)
    item.pop("lure", None)
    placement = item.get("placement") if isinstance(item.get("placement"), dict) else None
    out["entities"].append({
        "kind": "honeytoken",
        "id": token_id,
        "name": item.get("name"),
        "class": item.get("honeytoken_class") or item.get("asset_class"),
        "status": item.get("status"),
        "severity": item.get("severity"),
        "account_id": item.get("account_id"),
        "region": item.get("region"),
        "placement": _cap({k: placement.get(k) for k in ("type", "resource_arn", "container", "tf_address", "detail", "repo") if k in placement}) if placement else None,
        "action_config": _cap(item.get("action_config")),
    })
    out["summary"] = f"{item.get('honeytoken_class') or 'honeytoken'} {item.get('name') or token_id}, status {item.get('status')}"
    try:
        from captains.honeypot.incidents import list_incidents

        related = [i for i in list_incidents(table, pk, limit=50) if str(token_id) in [str(t) for t in (i.get("honeytoken_ids") or [])]]
        for incident in related[:3]:
            summary = incident.get("summary") if isinstance(incident.get("summary"), dict) else {}
            out["evidence"].append({
                "source": "incident",
                "value": {
                    "incident_id": incident.get("incident_id"),
                    "status": incident.get("status"),
                    "alert_count": incident.get("alert_count"),
                    "source_ip": incident.get("source_ip"),
                    "last_event_time": incident.get("last_event_time"),
                    "summary": _cap(summary.get("paragraph")),
                },
            })
        if any(str(i.get("status")) == "open" for i in related):
            out["changed_since_pr"].append("an incident on this honeytoken is open")
    except Exception as exc:
        out["notes"].append(f"incident lookup failed: {exc}")
    status = str(item.get("status") or "")
    if status and status not in ("pending_merge", "pending_pr", "draft"):
        out["changed_since_pr"].append(f"honeytoken status is {status}")
    return out


def _iac_context(record: Dict[str, Any], *, config: Dict[str, Any], workspace_id: str) -> Dict[str, Any]:
    out = _empty()
    origin = record.get("origin") or {}
    resource_ids = [str(r) for r in ((record.get("intent") or {}).get("scope") or {}).get("resource_ids") or origin.get("resource_ids") or []]
    org_id = str(config.get("org_id") or config.get("org") or workspace_id)
    if resource_ids:
        try:
            from infra_map.graph_store import open_store

            store = open_store({**config, "dynamodb_config": _dynamodb_config(config)}, workspace_id=workspace_id)
            for rid in resource_ids[:25]:
                node = store.get_node(org_id, rid)
                if node is None:
                    out["changed_since_pr"].append(f"{rid} is no longer in the map")
                    continue
                attrs = node.attributes if isinstance(getattr(node, "attributes", None), dict) else {}
                out["entities"].append({
                    "kind": "resource",
                    "id": node.resource_id,
                    "type": node.type,
                    "name": node.name,
                    "region": getattr(node, "region", None),
                    "drift_fields": attrs.get("drift_fields"),
                    "managed": attrs.get("managed") if "managed" in attrs else attrs.get("tf_managed"),
                    "attributes": _cap({k: attrs.get(k) for k in list(attrs)[:12]}),
                })
        except Exception as exc:  # noqa: BLE001 - no map store for this workspace, or it cannot open
            out["notes"].append(f"map lookup failed: {exc}")
    job_id = origin.get("job_id")
    if job_id:
        try:
            from compass_core.queue import job_queue

            job = job_queue().get_job(str(job_id), workspace_id=workspace_id) or {}
            result = job.get("result_summary") if isinstance(job.get("result_summary"), dict) else {}
            if result:
                out["evidence"].append({"source": "import_job", "value": _cap({k: result.get(k) for k in ("verified", "plan_clean", "warnings", "resource_count", "skipped_count", "files") if k in result})})
        except Exception as exc:
            out["notes"].append(f"job lookup failed: {exc}")
    out["summary"] = f"{len(out['entities'])} of {len(resource_ids)} resource(s) found in the map" if resource_ids else None
    return out


_PROVIDERS = {
    "engine": _finding_context,
    "finops_engine": _finding_context,
    "honeypot": _honeypot_context,
    "iac": _iac_context,
}


def load_origin_context(record: Dict[str, Any], *, config: Dict[str, Any], workspace_id: str) -> Dict[str, Any]:
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    captain = str(origin.get("captain") or "")
    merged = _empty()
    merged["origin"] = {k: origin.get(k) for k in ("captain", "command", "finding_id", "rope_id", "honeytoken_id", "job_id", "user_id")}
    providers = []
    if captain in _PROVIDERS:
        providers.append(_PROVIDERS[captain])
    else:
        merged["notes"].append(f"no context provider for captain {captain!r}")
    if origin.get("rope_id"):
        providers.append(_rope_context)
    for provider in providers:
        try:
            part = provider(record, config=config or {}, workspace_id=workspace_id)
        except Exception as exc:  # never block a revision on context
            logger.warning("[prs.context] %s failed: %s", getattr(provider, "__name__", provider), exc)
            merged["notes"].append(f"{getattr(provider, '__name__', 'provider')} failed: {exc}")
            continue
        for key in ("entities", "evidence", "history", "changed_since_pr", "notes"):
            merged[key].extend(part.get(key) or [])
        if part.get("summary") and not merged["summary"]:
            merged["summary"] = part["summary"]
    return merged
