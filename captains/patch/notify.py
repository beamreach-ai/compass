"""``emit(event)`` for the patch captain (docs/specs/patching-captain.md §3.8), through today's
routing: ``config.notifications.routing = {event: [endpoint_id]}``, with ``patch.*`` as the
fallback key. No route means no message; the job event is always written."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from compass_core.events import emit_job_event

logger = logging.getLogger("patch.notify")

SEVERITY = {"patch.pr_opened": "medium", "patch.pr_opened_exploited": "high", "patch.needs_human": "high", "patch.deployed": "info"}


def _subject(event: str, payload: Dict[str, Any]) -> str:
    ref = f"{payload.get('repo')}#{payload.get('number')}" if payload.get("number") else str(payload.get("repo") or "")
    return {
        "patch.pr_opened": f"Compass opened a security fix on {ref}: {payload.get('advisories') or ''}".rstrip(": "),
        "patch.needs_human": f"Compass needs you on {ref}: {str(payload.get('reason') or '').replace('_', ' ')}",
        "patch.deployed": f"The fix for {payload.get('advisory_id') or 'a finding'} is running on {payload.get('workload') or ref}",
    }.get(event, f"{event} {ref}")


def routes_for(config: Dict[str, Any], event: str) -> List[str]:
    notifications = config.get("notifications") if isinstance(config.get("notifications"), dict) else {}
    routing = notifications.get("routing") if isinstance(notifications.get("routing"), dict) else {}
    ids = routing.get(event)
    if ids is None:
        ids = routing.get("patch.*")
    return [str(i) for i in (ids or []) if i]


def emit(config: Dict[str, Any], workspace_id: str, event: str, payload: Dict[str, Any], *, sender: Any = None) -> int:
    """Returns how many endpoints accepted the message."""
    severity = SEVERITY.get(f"{event}_exploited" if event == "patch.pr_opened" and payload.get("kev") else event, SEVERITY.get(event, "info"))
    try:
        emit_job_event(_subject(event, payload), event_type="info", data={"event": event, "severity": severity, **payload})
    except Exception:  # noqa: BLE001 - outside a job there is no event to write
        pass
    endpoint_ids = routes_for(config, event)
    if not endpoint_ids:
        return 0
    try:
        from captains.prs.notify import _load_endpoints

        if sender is None:
            from captains.honeypot.alert_dispatcher import _send as sender  # type: ignore[no-redef]
        endpoints = _load_endpoints(config, workspace_id, endpoint_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[patch.notify] endpoints not loaded: %s", exc)
        return 0
    delivered = 0
    text = "\n".join(f"{k}: {v}" for k, v in payload.items() if v not in (None, "", [], {}))
    billing = config.get("billing") if isinstance(config.get("billing"), dict) else {}
    for endpoint in endpoints:
        try:
            sender(endpoint, subject=_subject(event, payload), text_body=text, html_body=None, severity=severity,
                   relay_license_key=billing.get("license_key"), relay_api_url=os.getenv("LICENSING_API_URL"))
            delivered += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[patch.notify] %s to %s failed: %s", event, endpoint.get("endpoint_id"), exc)
    return delivered
