"""Recording a honeytoken trip, independent of how it arrived.

Alerts reach Compass over SQS — an EventBridge rule in the target account
delivers cross-account to a queue the control plane owns. There is deliberately
no internet-facing path: the queue policy names the exact source rule, so
authentication is IAM and there is no shared secret to rotate or leak.

The HTTP endpoint still exists for testing, but it is authenticated and is not
the production route. Both call into here so the two can never drift — an alert
recorded over SQS and one recorded over HTTP produce identical state.

Delivery over a queue also changes the failure mode in the way that matters for
a honeypot: if Compass is down or mid-deploy, alerts wait rather than
disappearing. An intrusion signal that is dropped because the receiver was
restarting is worse than one that arrives a minute late.
"""

from __future__ import annotations

import ipaddress
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("honeypot-ingestion")

# "Internal" means plausibly our own network, so the ranges are listed
# explicitly rather than deferring to `ipaddress.is_private`.
#
# `is_private` is broader than it sounds: it is true for the documentation
# ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) and for link-local
# 169.254.0.0/16. Using it would have classified 203.0.113.7 — the address the
# test-fire path sends, and one that can only ever be synthetic — as an
# internal source, which is precisely backwards. On a real alert the same flaw
# would tell a responder the call came from inside the network when it did not.
_INTERNAL_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fd00::/8")
)


def classify_ip(source_ip: Optional[str]) -> Optional[str]:
    """"internal", "external", "aws_service", or None if undeterminable.

    Deliberately conservative: anything not demonstrably inside our own address
    space is reported as external. Under-calling an intrusion is far worse than
    over-calling one.
    """
    if not source_ip:
        return None
    candidate = source_ip.strip()

    # CloudTrail puts service principals in sourceIPAddress as well as
    # addresses — "s3.amazonaws.com" means AWS acted, not a person.
    if candidate.endswith(".amazonaws.com"):
        return "aws_service"

    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    return "internal" if any(addr in net for net in _INTERNAL_NETWORKS) else "external"


def record_alert(
    table: Any,
    pk: str,
    payload: Dict[str, Any],
    *,
    workspace_id: str,
    queue_client: Any = None,
    is_test: bool = False,
    placement_summary: Any = None,
    get_placement: Any = None,
    to_native: Any = None,
    received_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist one alert, bump the honeytoken's counters, and queue notification.

    Returns the stored item. Notification failure is logged rather than raised:
    the alert itself is already durable at that point, and losing the record
    because Slack was unreachable would be the wrong trade.
    """
    # EventBridge renders absent paths as "", so "" means missing everywhere below.
    payload = {k: v for k, v in payload.items() if v != ""}

    # Plants deployed before the rename still send `asset_id`; the rule
    # templates and listeners send `token_id`.
    honeytoken_id = str(payload.get("honeytoken_id") or payload.get("token_id") or payload.get("asset_id") or "")
    if not honeytoken_id:
        raise ValueError("honeytoken_id is required")

    from captains.honeypot.classes import normalize_class
    from compass_core.keys import honeypot_alert_sort_key, honeytoken_sort_key

    alert_id = str(uuid.uuid4())
    processed_at = datetime.now(timezone.utc).isoformat()
    event_time = payload.get("event_time") or None
    # event_time: when it happened (CloudTrail / listener); received_at: when it
    # reached the queue (SQS SentTimestamp); processed_at: when we recorded it.
    triggered_at = event_time or processed_at
    source_ip = payload.get("source_ip")

    item: Dict[str, Any] = {
        "pk": pk,
        "sk": honeypot_alert_sort_key(honeytoken_id, alert_id),
        "alert_id": alert_id,
        "honeytoken_id": honeytoken_id,
        "triggered_at": triggered_at,
        "processed_at": processed_at,
    }
    if event_time:
        item["event_time"] = event_time
    if received_at:
        item["received_at"] = received_at
    # A test-fire from Compass arrives through the real path with a marked
    # user agent; it is recorded as a test and never opens an incident.
    if "compass-test-fire" in str(payload.get("user_agent") or ""):
        is_test = True
    if is_test:
        item["is_test"] = True
    is_probe = payload.get("kind") == "probe"
    if payload.get("kind"):
        item["kind"] = str(payload["kind"])
    if is_probe:
        item["severity"] = "low"
    task_id = payload.get("source_task_id") or payload.get("task_id")
    if task_id:
        item["source_task_id"] = str(task_id)

    # Derive ip_type when the sender did not supply one. EventBridge input
    # transformers cannot classify an address, so this is the normal case for a
    # real trip rather than an edge case.
    ip_type = payload.get("ip_type") or classify_ip(source_ip)
    for key, value in (
        ("source_ip", source_ip),
        ("ip_type", ip_type),
        ("principal_arn", payload.get("principal_arn")),
        ("user_agent", payload.get("user_agent")),
        ("service", payload.get("service")),
        ("event_id", payload.get("event_id")),
        ("event_name", payload.get("event_name")),
        ("region", payload.get("region")),
        ("access_key_id", payload.get("access_key_id")),
        ("error_code", payload.get("error_code")),
        ("vpc_endpoint_id", payload.get("vpc_endpoint_id")),
        ("raw_event", payload.get("raw_event")),
    ):
        if value:
            item[key] = value

    # The token is loaded once: placement groups the alert, the rest feeds the
    # notification.
    honeytoken: Dict[str, Any] = {}
    try:
        honeytoken = (table.get_item(Key={"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)}) or {}).get("Item") or {}
    except Exception as exc:
        logger.warning("Could not load honeytoken %s: %s", honeytoken_id, exc)
    placement = (to_native(honeytoken.get("placement")) if to_native else honeytoken.get("placement")) or None
    if placement:
        item["placement"] = placement
        # Name and class travel with the alert so summaries and reports need
        # no second lookup.
        item["honeytoken_name"] = str(honeytoken.get("name", ""))
        item["honeytoken_class"] = normalize_class(honeytoken.get("honeytoken_class") or honeytoken.get("asset_class") or "")

    table.put_item(Item=item)
    table.update_item(
        Key={"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)},
        UpdateExpression=(
            "SET last_triggered_at = :t, "
            "trigger_count = if_not_exists(trigger_count, :zero) + :one"
        ),
        ExpressionAttributeValues={":t": triggered_at, ":zero": 0, ":one": 1},
    )

    # One incident per intrusion, one notification per incident. If grouping
    # fails the alert is still stored and still notified: a duplicate message
    # is better than a silent one.
    incident: Optional[Dict[str, Any]] = None
    is_new = True
    try:
        from captains.honeypot.incidents import attach_alert

        if is_test:
            is_new = False  # a test never becomes an incident
        else:
            incident, is_new = attach_alert(table, pk, item, placement=placement, now_ms=int(time.time() * 1000))
        if incident:
            table.update_item(
                Key={"pk": pk, "sk": item["sk"]},
                UpdateExpression="SET incident_id = :i",
                ExpressionAttributeValues={":i": incident["incident_id"]},
            )
            item["incident_id"] = incident["incident_id"]
    except Exception as exc:
        logger.warning("Alert %s stored but could not be grouped into an incident: %s", alert_id, exc)

    if is_probe or is_test:
        is_new = False

    if queue_client is not None and is_new:
        try:
            action_config = to_native(honeytoken.get("action_config") or {}) if to_native else (honeytoken.get("action_config") or {})
            note = placement_summary(get_placement(table, pk, honeytoken_id)) if (placement_summary and get_placement) else ""
            queue_client.enqueue(
                captain="honeypot",
                command="alert_notify",
                payload={
                    "alert_id": alert_id,
                    "incident_id": (incident or {}).get("incident_id"),
                    "repeat_count": (incident or {}).get("repeat_count", 1),
                    "related_incident_id": (incident or {}).get("related_incident_id"),
                    "honeytoken_id": honeytoken_id,
                    "honeytoken_name": str(honeytoken.get("name", "")),
                    "honeytoken_class": normalize_class(honeytoken.get("honeytoken_class") or honeytoken.get("asset_class") or ""),
                    "source_ip": source_ip,
                    "ip_type": ip_type,
                    "principal_arn": payload.get("principal_arn"),
                    "triggered_at": triggered_at,
                    "event_time": event_time,
                    "received_at": received_at,
                    "processed_at": processed_at,
                    "action_config": action_config,
                    "placement_note": note,
                    "is_test": is_test,
                    "workspace_id": workspace_id,
                },
            )
        except Exception as exc:
            # The alert is already stored; a notification failure must not undo
            # that or fail the SQS receipt, or the message would redeliver and
            # duplicate the alert.
            logger.warning("Alert %s stored but notification could not be queued: %s", alert_id, exc)
        # Investigation (correlation, blast radius) runs as its own job so the
        # notification is never delayed by AWS lookups.
        try:
            queue_client.enqueue(
                captain="honeypot",
                command="investigate",
                payload={
                    "incident_id": (incident or {}).get("incident_id"),
                    "region": payload.get("region") or payload.get("aws_region"),
                    "workspace_id": workspace_id,
                },
            )
        except Exception as exc:
            logger.warning("Alert %s stored but investigation could not be queued: %s", alert_id, exc)

    logger.info("Recorded honeypot alert %s for honeytoken %s (%s)", alert_id, honeytoken_id, ip_type or "unknown source")
    return item
