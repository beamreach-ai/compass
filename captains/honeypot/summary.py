"""HP-09: the AI summary of an incident.

One model call, no tools. The model sees ``build_incident_facts`` and nothing
else; it writes prose only. It never sets risk and never chooses an action
(that is a separate call, HP-10). The result is cached on the incident.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("honeypot-summary")

PROMPT_PATH = Path(__file__).parent / "prompts" / "incident_summary.md"
MODEL_TIMEOUT_SECONDS = 10
ISOLATE_ACTION = "ecs_isolate_service"
RECOMMENDATION = "isolate the service (not enabled)"

# (incident field, label used in the facts), in timeline order.
_STEPS = (
    ("first_received_at", "received"),
    ("first_alert_sent_at", "alert_sent"),
    ("investigation_done_at", "investigation_done"),
    ("action_chosen_at", "action_chosen"),
    ("action_started_at", "action_started"),
    ("action_verified_at", "action_verified"),
    ("report_sent_at", "report_sent"),
    ("closed_at", "closed"),
)

# The exact top-level keys of the facts (asserted in tests).
FACT_KEYS = (
    "incident_id", "status", "alert_count", "honeytokens", "workload", "source",
    "timeline", "blast_radius", "correlation", "actions", "repeat",
)


def _parse(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def timeline_deltas(incident: Dict[str, Any]) -> Dict[str, float]:
    """Seconds from the first event to each known step."""
    origin = _parse(incident.get("first_event_time"))
    out: Dict[str, float] = {}
    if origin is None:
        return out
    for field, label in _STEPS:
        at = _parse(incident.get(field))
        if at is not None:
            out[label] = round((at - origin).total_seconds(), 1)
    return out


def _last(arn: Any) -> Optional[str]:
    return str(arn).split("/")[-1] if arn else None


def _action_facts(audit: Dict[str, Any]) -> Dict[str, Any]:
    verify = audit.get("verify") or {}
    executed = audit.get("executed") or {}
    target = audit.get("target") or {}
    rollback = audit.get("rollback") or {}
    return {
        "type": audit.get("action_type"),
        "status": audit.get("status"),
        "snapshot_taken": bool(audit.get("snapshot")),
        "executed": bool(executed),
        "verified": (verify.get("ok") if verify else None),
        "old_task_id": _last(executed.get("stopped_task") or target.get("task_arn")),
        "new_task_id": _last(verify.get("new_task")),
        "rolled_back": bool(rollback.get("done_at")),
    }


def build_incident_facts(
    incident: Dict[str, Any], alerts: List[Dict[str, Any]], audits: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """The only input the model sees. Never includes raw events or key material."""
    placement = incident.get("placement") or {}
    tokens, seen = [], set()
    for a in alerts:
        tid = a.get("honeytoken_id")
        if tid and tid not in seen:
            seen.add(tid)
            tokens.append({
                "name": a.get("honeytoken_name") or tid,
                "class": a.get("honeytoken_class") or None,
            })
    for tid in incident.get("honeytoken_ids") or []:
        if tid not in seen:
            seen.add(tid)
            tokens.append({"name": tid, "class": None})
    first = alerts[0] if alerts else {}
    actions = [_action_facts(a) for a in audits]
    repeat_count = int(incident.get("repeat_count") or 1)
    isolated = any(x["type"] == ISOLATE_ACTION and x["status"] != "refused" for x in actions)
    restarts = sum(1 for x in actions if x["type"] == "ecs_restart_task")
    repeat: Dict[str, Any] = {
        "repeat_count": repeat_count,
        "related_incident_id": incident.get("related_incident_id"),
    }
    if repeat_count >= 2 and not isolated:
        repeat["recommended"] = RECOMMENDATION
    return {
        "incident_id": incident.get("incident_id"),
        "status": incident.get("status"),
        "alert_count": int(incident.get("alert_count") or len(alerts)),
        "honeytokens": tokens,
        "workload": {
            "service": _last(incident.get("service_arn") or placement.get("service_arn")),
            "container": placement.get("container") or placement.get("container_name"),
            "task_id": _last(incident.get("source_task_id")),
        },
        "source": {
            "ip": incident.get("source_ip") or first.get("source_ip"),
            "ip_type": first.get("ip_type"),
            "task_id": _last(incident.get("source_task_id") or first.get("source_task_id")),
        },
        "timeline": {"first_event_time": incident.get("first_event_time"), "seconds_after_first_event": timeline_deltas(incident)},
        "blast_radius": incident.get("blast_radius"),
        "correlation": incident.get("correlation"),
        "actions": actions,
        "repeat": {**repeat, "restarts_in_this_incident": restarts},
    }


def _load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def default_invoke(system_prompt: str, facts_json: str) -> str:
    from compass_core.llm import model_for

    model = model_for()
    if model is None:
        raise RuntimeError("no language model is configured for the incident summary (compass_core.llm.set_model_factory)")
    return model.call(
        [{"role": "user", "content": f"Facts:\n{facts_json}"}],
        max_tokens=800,
        temperature=0,
        timeout=MODEL_TIMEOUT_SECONDS,
        system_prompt=system_prompt,
    )


def _model_name() -> str:
    try:
        from compass_core.llm import model_for, model_name

        return model_name(model_for())
    except Exception:  # noqa: BLE001
        return "model"


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def parse_summary(text: Any) -> Dict[str, Any]:
    raw = text if isinstance(text, str) else json.dumps(text)
    raw = _FENCE.sub("", raw.strip()).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model did not return JSON")
        data = json.loads(raw[start : end + 1])
    paragraph, bullets = data.get("paragraph"), data.get("bullets")
    if not isinstance(paragraph, str) or not paragraph.strip() or not isinstance(bullets, list):
        raise ValueError("model JSON lacks paragraph or bullets")
    return {"paragraph": paragraph.strip(), "bullets": [str(b).strip() for b in bullets if str(b).strip()][:5]}


def summarize_incident(facts: Dict[str, Any], *, invoke: Optional[Callable[[str, str], Any]] = None) -> Dict[str, Any]:
    """One call, no tools. ``invoke(system_prompt, facts_json) -> text``."""
    call = invoke or default_invoke
    text = call(_load_prompt(), json.dumps(facts, indent=2, default=str))
    out = parse_summary(text)
    out["model"] = "injected" if invoke else _model_name()
    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    return out


# Route-level injection point (tests monkeypatch this); None means the Bedrock default.
INVOKE: Optional[Callable[[str, str], Any]] = None
