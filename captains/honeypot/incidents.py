"""Incidents: the alerts of one intrusion, grouped, with a timeline (HP-07).

Grouping key: (placement.service_arn, else the honeytoken id) plus the source
task id when the alert carries one, else the source IP. A new alert joins the
newest incident with that key that is open and not yet contained; otherwise it
opens a new one. A trip after containment was verified therefore opens a
second incident, linked to the first by ``related_incident_id``.

Timeline fields are only stored once known (ISO-8601 strings), so a missing
field means "has not happened yet". HP-08 fills ``correlation`` and
``blast_radius``, HP-09 ``summary``, HP-10/11 ``action`` and ``audit_ids``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from boto3.dynamodb.conditions import Attr, Key

from compass_core.keys import honeypot_incident_prefix, honeypot_incident_sort_key

logger = logging.getLogger("honeypot-incidents")

ALERT_ID_CAP = 200
REPEAT_WINDOW_MS = 7 * 24 * 3600 * 1000

# In timeline order.
TIMELINE_FIELDS = (
    "first_event_time",
    "first_received_at",
    "first_alert_sent_at",
    "investigation_done_at",
    "action_chosen_at",
    "action_started_at",
    "action_verified_at",
    "report_sent_at",
    "closed_at",
)

# Fields mark() will write. Anything else is refused so a typo cannot create a
# stray attribute.
MARKABLE_FIELDS = set(TIMELINE_FIELDS) | {
    "correlation", "blast_radius", "summary", "action", "source_task_id", "cluster_arn", "service_arn",
    "status", "audit_ids", "last_event_time", "report_url", "slack_thread", "notified_endpoint_ids",
    "resolution",  # why it was closed when not by a person ("demo reset")
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_incident_id(now_ms: int) -> str:
    return f"{int(now_ms)}-{uuid.uuid4().hex[:8]}"


def incident_created_ms(incident: Dict[str, Any]) -> int:
    try:
        return int(str(incident.get("incident_id", "")).split("-", 1)[0])
    except ValueError:
        return 0


def grouping_key_for(alert: Dict[str, Any], placement: Optional[Dict[str, Any]]) -> str:
    anchor = (placement or {}).get("service_arn") or alert.get("honeytoken_id") or ""
    source = alert.get("source_task_id") or alert.get("source_ip") or ""
    return f"{anchor}|{source}"


def _clean(item: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in item.items() if k not in ("pk", "sk")}
    return out


def _query_incidents(table: Any, pk: str, *, limit: Optional[int] = None):
    """Yield incident items newest first, following pagination."""
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(honeypot_incident_prefix()),
        "ScanIndexForward": False,
    }
    seen = 0
    while True:
        resp = table.query(**kwargs)
        for item in resp.get("Items", []):
            yield item
            seen += 1
            if limit is not None and seen >= limit:
                return
        last = resp.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def attach_alert(
    table: Any,
    pk: str,
    alert: Dict[str, Any],
    *,
    placement: Optional[Dict[str, Any]],
    now_ms: int,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Group `alert` into an incident. Returns (incident, is_new).

    Probes (the decoy database listener's port scans) are never grouped: they
    get ``severity = "low"`` and ``(None, False)``.
    """
    if alert.get("kind") == "probe":
        alert["severity"] = "low"
        return None, False

    key = grouping_key_for(alert, placement)
    event_time = alert.get("event_time") or alert.get("triggered_at") or now_iso()
    alert_id = alert["alert_id"]
    honeytoken_id = alert.get("honeytoken_id")

    # Alerts join an open, uncontained incident on the exact key (service and
    # source task). Repeats are linked by the service alone: after a
    # containment the replacement task has a new id and IP, and "a second
    # attempt against the same service" is exactly what the link must say.
    anchor = key.split("|", 1)[0]
    joinable: Optional[Dict[str, Any]] = None
    previous: Optional[Dict[str, Any]] = None
    for item in _query_incidents(table, pk):
        item_key = str(item.get("grouping_key") or "")
        if item_key.split("|", 1)[0] != anchor:
            continue
        if previous is None:
            previous = item
        if item_key == key and not item.get("closed_at") and not item.get("action_verified_at"):
            joinable = item
            break

    if joinable is not None:
        ids = list(joinable.get("honeytoken_ids") or [])
        if honeytoken_id and honeytoken_id not in ids:
            ids.append(honeytoken_id)
        alert_ids = list(joinable.get("alert_ids") or [])
        sets = ["alert_count = alert_count + :one", "last_event_time = :t", "honeytoken_ids = :h"]
        values: Dict[str, Any] = {":one": 1, ":t": event_time, ":h": ids}
        if len(alert_ids) < ALERT_ID_CAP:
            sets.append("alert_ids = list_append(alert_ids, :a)")
            values[":a"] = [alert_id]
            alert_ids.append(alert_id)
        if alert.get("source_task_id") and not joinable.get("source_task_id"):
            sets.append("source_task_id = :task")
            values[":task"] = alert["source_task_id"]
        table.update_item(
            Key={"pk": pk, "sk": joinable["sk"]},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeValues=values,
        )
        joinable = dict(joinable)
        joinable["alert_count"] = int(joinable.get("alert_count", 0)) + 1
        joinable["honeytoken_ids"] = ids
        joinable["alert_ids"] = alert_ids
        joinable["last_event_time"] = event_time
        return _clean(joinable), False

    incident_id = new_incident_id(now_ms)
    placement = dict(placement or {})
    # No annotation: Cython (release obfuscation build) rejects re-annotating a name
    # already bound earlier in this function ('item' redeclared).
    item = {
        "pk": pk,
        "sk": honeypot_incident_sort_key(incident_id),
        "incident_id": incident_id,
        "status": "open",
        "grouping_key": key,
        "honeytoken_ids": [honeytoken_id] if honeytoken_id else [],
        "placement": placement,
        "alert_count": 1,
        "alert_ids": [alert_id],
        "first_event_time": event_time,
        "last_event_time": event_time,
        "repeat_count": 1,
        "correlation": None,
        "blast_radius": None,
        "summary": None,
        "action": None,
        "audit_ids": [],
    }
    for field, value in (
        ("service_arn", placement.get("service_arn")),
        ("cluster_arn", placement.get("cluster_arn")),
        ("source_task_id", alert.get("source_task_id")),
        ("source_ip", alert.get("source_ip")),
        ("first_received_at", alert.get("received_at")),
    ):
        if value:
            item[field] = value
    # A closed incident ends the chain: someone reviewed it, so the next trip
    # starts a new story rather than counting as attempt N+1 of an old one.
    if (
        previous is not None
        and not previous.get("closed_at")
        and now_ms - incident_created_ms(previous) <= REPEAT_WINDOW_MS
    ):
        item["related_incident_id"] = previous["incident_id"]
        item["repeat_count"] = int(previous.get("repeat_count", 1)) + 1
    table.put_item(Item=item, ConditionExpression=Attr("pk").not_exists())
    return _clean(item), True



def dynamo_safe(value: Any) -> Any:
    """DynamoDB rejects floats and empty strings inside maps; the pipeline's
    results (verify timings, decisions) carry both."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: dynamo_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [dynamo_safe(v) for v in value]
    if value == "":
        return None
    return value


def mark(table: Any, pk: str, incident_id: str, **fields: Any) -> None:
    """Set timeline (or other whitelisted) fields on an incident.

    `action_verified_at` also moves the status to "contained" unless the
    incident is already closed; `closed_at` moves it to "closed".
    """
    unknown = set(fields) - MARKABLE_FIELDS
    if unknown:
        raise ValueError(f"unknown incident fields: {sorted(unknown)}")
    if not fields:
        return
    if fields.get("closed_at"):
        fields.setdefault("status", "closed")
    elif fields.get("action_verified_at"):
        fields.setdefault("status", "contained")
    names = {f"#f{i}": k for i, k in enumerate(fields)}
    values = {f":v{i}": dynamo_safe(v) for i, v in enumerate(fields.values())}
    expr = "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields)))
    cond = Attr("pk").exists()
    if "status" in fields and fields["status"] != "closed" and not fields.get("closed_at"):
        # A late status change must not reopen a closed incident.
        cond = cond & Attr("closed_at").not_exists()
    table.update_item(
        Key={"pk": pk, "sk": honeypot_incident_sort_key(incident_id)},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ConditionExpression=cond,
    )


def mark_alert_sent(table: Any, pk: str, incident_id: str, sent_at: str) -> None:
    """Record when the first notification went out. First write wins."""
    try:
        table.update_item(
            Key={"pk": pk, "sk": honeypot_incident_sort_key(incident_id)},
            UpdateExpression="SET first_alert_sent_at = :s",
            ConditionExpression=Attr("pk").exists() & Attr("first_alert_sent_at").not_exists(),
            ExpressionAttributeValues={":s": sent_at},
        )
    except Exception as exc:  # noqa: BLE001 - already set, or incident gone
        if "ConditionalCheckFailed" not in type(exc).__name__ and "ConditionalCheckFailed" not in str(exc):
            raise


def get_incident(table: Any, pk: str, incident_id: str) -> Optional[Dict[str, Any]]:
    item = (table.get_item(Key={"pk": pk, "sk": honeypot_incident_sort_key(incident_id)}) or {}).get("Item")
    return _clean(item) if item else None


def list_incidents(table: Any, pk: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Newest first."""
    return [_clean(i) for i in _query_incidents(table, pk, limit=max(1, int(limit)))]


def alerts_for_incident(table: Any, pk: str, incident_id: str) -> List[Dict[str, Any]]:
    out = _filtered_query(table, pk, "HONEYPOT#ALERT#TOKEN#", incident_id)
    out.sort(key=lambda a: str(a.get("triggered_at", "")))
    return out


def audits_for_incident(table: Any, pk: str, incident_id: str) -> List[Dict[str, Any]]:
    from compass_core.keys import honeypot_audit_prefix

    out = _filtered_query(table, pk, honeypot_audit_prefix(), incident_id)
    out.sort(key=lambda a: str(a.get("audit_id", "")))
    return out


def _filtered_query(table: Any, pk: str, prefix: str, incident_id: str) -> List[Dict[str, Any]]:
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(prefix),
        "FilterExpression": Attr("incident_id").eq(incident_id),
    }
    out: List[Dict[str, Any]] = []
    while True:
        resp = table.query(**kwargs)
        out.extend(_clean(i) for i in resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return out
        kwargs["ExclusiveStartKey"] = last
