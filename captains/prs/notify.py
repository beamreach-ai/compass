"""``emit(event)`` for the PR watcher (agent-surface spec §5.4, the part this feature needs).

Routing lives at ``config.notifications.routing = {event: [endpoint_id]}``;
endpoints are the ``NOTIFY#ENDPOINT#`` items the Honeypots feature already
manages, delivered through its dispatcher. No route for an event means no
message; the job event is always written.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from compass_core.events import emit_job_event
from compass_core.keys import notification_endpoint_sort_key, workspace_pk
from compass_core.tables import tables

logger = logging.getLogger("prs.notify")

SEVERITY = {
    "pr.needs_human": "high",
    "pr.ci_failed": "medium",
    "pr.changes_requested": "low",
    "pr.revised": "low",
    "pr.ready": "low",
    "pr.merged": "info",
    "pr.closed": "info",
}


def _subject(event: str, payload: Dict[str, Any]) -> str:
    ref = f"{payload.get('repo')}#{payload.get('number')}"
    return {
        "pr.needs_human": f"Compass needs you on {ref}: {str(payload.get('reason') or '').replace('_', ' ')}",
        "pr.ci_failed": f"Checks failed on {ref}",
        "pr.changes_requested": f"Review feedback on {ref}",
        "pr.revised": f"Compass pushed a revision to {ref}",
        "pr.ready": f"{ref} is ready to merge",
        "pr.merged": f"{ref} merged",
        "pr.closed": f"{ref} closed without merging",
    }.get(event, f"{event} {ref}")


def _text(event: str, payload: Dict[str, Any]) -> str:
    lines = [f"{k}: {v}" for k, v in payload.items() if v not in (None, "", [], {})]
    url = payload.get("url")
    if url:
        lines.append(str(url))
    return "\n".join(lines) or event


def _load_endpoints(config: Dict[str, Any], workspace_id: str, endpoint_ids: List[str]) -> List[Dict[str, Any]]:
    table_name = tables(config).items
    if not table_name or not endpoint_ids:
        return []
    import boto3

    table = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")).Table(table_name)
    pk = workspace_pk(workspace_id)
    out = []
    for endpoint_id in endpoint_ids:
        try:
            item = table.get_item(Key={"pk": pk, "sk": notification_endpoint_sort_key(str(endpoint_id))}).get("Item")
        except Exception as exc:
            logger.warning("[prs.notify] endpoint %s lookup failed: %s", endpoint_id, exc)
            continue
        if item:
            out.append(dict(item))
    return out


def routes_for(config: Dict[str, Any], event: str) -> List[str]:
    notifications = config.get("notifications") if isinstance(config.get("notifications"), dict) else {}
    routing = notifications.get("routing") if isinstance(notifications.get("routing"), dict) else {}
    ids = routing.get(event)
    if ids is None:
        ids = routing.get("pr.*")
    return [str(i) for i in (ids or []) if i]


def emit(config: Dict[str, Any], workspace_id: str, event: str, payload: Dict[str, Any], *, sender=None) -> int:
    """Returns how many endpoints accepted the message."""
    emit_job_event(_subject(event, payload), event_type="info", data={"event": event, **payload})
    endpoint_ids = routes_for(config, event)
    if not endpoint_ids:
        return 0
    if sender is None:
        from captains.honeypot.alert_dispatcher import _send as sender  # type: ignore[no-redef]
    endpoints = _load_endpoints(config, workspace_id, endpoint_ids)
    delivered = 0
    billing = config.get("billing") if isinstance(config.get("billing"), dict) else {}
    for endpoint in endpoints:
        try:
            sender(
                endpoint,
                subject=_subject(event, payload),
                text_body=_text(event, payload),
                html_body=None,
                severity=SEVERITY.get(event, "low"),
                relay_license_key=billing.get("license_key"),
                relay_api_url=os.getenv("LICENSING_API_URL"),
            )
            delivered += 1
        except Exception as exc:
            logger.warning("[prs.notify] %s to %s failed: %s", event, endpoint.get("endpoint_id"), exc)
    return delivered
