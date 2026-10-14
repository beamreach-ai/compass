"""HP-09: the incident report as Markdown (Slack thread, email, UI page).

Built from the incident, its alerts and audits, and the cached summary.
Carries no secret material: only ids, names and counts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from captains.honeypot.summary import ISOLATE_ACTION, RECOMMENDATION, _action_facts, build_incident_facts, timeline_deltas

_LABELS = {
    "received": "Received", "alert_sent": "Alert sent", "investigation_done": "Investigation done",
    "action_chosen": "Action chosen", "action_started": "Action started", "action_verified": "Action verified",
    "report_sent": "Report sent", "closed": "Closed",
}
_FIELD_OF = {
    "received": "first_received_at", "alert_sent": "first_alert_sent_at", "investigation_done": "investigation_done_at",
    "action_chosen": "action_chosen_at", "action_started": "action_started_at", "action_verified": "action_verified_at",
    "report_sent": "report_sent_at", "closed": "closed_at",
}


def repeat_note(incident: Dict[str, Any], audits: List[Dict[str, Any]], prior_restarts: int = 0) -> Optional[str]:
    """``prior_restarts``: restarts run for the earlier attempts (earlier incidents)."""
    count = int(incident.get("repeat_count") or 1)
    if count < 2:
        return None
    if any(a.get("action_type") == ISOLATE_ACTION and a.get("status") != "refused" for a in audits):
        return None
    restarts = prior_restarts + sum(1 for a in audits if a.get("action_type") == "ecs_restart_task")
    return f"Repeat on the same service: {count} attempts, {restarts} restarts. Recommended: {RECOMMENDATION}"


def _cell(value: Any) -> str:
    return "" if value is None else str(value).replace("|", "\\|")


def render_markdown(
    incident: Dict[str, Any],
    alerts: List[Dict[str, Any]],
    audits: List[Dict[str, Any]],
    summary: Optional[Dict[str, Any]],
    prior_restarts: int = 0,
) -> str:
    facts = build_incident_facts(incident, alerts, audits)
    L: List[str] = [f"# Honeytoken incident ({incident.get('status')})", ""]

    L += ["## Summary", ""]
    if isinstance(summary, dict) and summary.get("paragraph"):
        L.append(summary["paragraph"])
        L.append("")
        L += [f"- {b}" for b in summary.get("bullets") or []]
    else:
        L.append("Summary pending.")
    L.append("")

    L += ["## Timeline", "", "| Step | Time | After first event |", "| --- | --- | --- |"]
    L.append(f"| First event | {_cell(incident.get('first_event_time'))} | +0s |")
    deltas = timeline_deltas(incident)
    for label, secs in deltas.items():
        L.append(f"| {_LABELS[label]} | {_cell(incident.get(_FIELD_OF[label]))} | +{secs}s |")
    L.append("")

    L += ["## Honeytokens", ""]
    for t in facts["honeytokens"] or []:
        L.append(f"- {t['name']}" + (f" ({t['class']})" if t.get("class") else ""))
    if not facts["honeytokens"]:
        L.append("None.")
    L.append("")

    w, s = facts["workload"], facts["source"]
    L += ["## Workload", ""]
    L += [f"- Service: `{w['service']}`" if w["service"] else "- Service: unknown"]
    if w.get("container"):
        L.append(f"- Container: `{w['container']}`")
    if w.get("task_id"):
        L.append(f"- Task: `{w['task_id']}`")
    L += ["", "## Source", ""]
    L.append(f"- IP: `{s['ip']}`" + (f" ({s['ip_type']})" if s.get("ip_type") else "") if s.get("ip") else "- IP: unknown")
    if s.get("task_id"):
        L.append(f"- Task: `{s['task_id']}`")
    L.append(f"- Alerts: {facts['alert_count']}")
    L.append("")

    L += ["## Blast radius", ""]
    br = facts["blast_radius"]
    if isinstance(br, dict) and br:
        for k, v in br.items():
            L.append(f"- {k.replace('_', ' ')}: {', '.join(map(str, v)) if isinstance(v, list) else v}")
    else:
        L.append("Not available.")
    if facts["correlation"]:
        L += ["", f"Correlation: {facts['correlation']}"]
    L.append("")

    L += ["## Actions and audit", ""]
    if not audits:
        L.append("No action taken.")
    for au in audits:
        a = _action_facts(au)
        L.append(f"- `{a['type']}`: {a['status']}")
        L.append(f"  - snapshot taken: {'yes' if a['snapshot_taken'] else 'no'}; executed: {'yes' if a['executed'] else 'no'}; "
                 f"verified: {'yes' if a['verified'] else ('failed' if a['verified'] is False else 'no')}")
        if a["old_task_id"] or a["new_task_id"]:
            L.append(f"  - task: `{a['old_task_id'] or '?'}` -> `{a['new_task_id'] or '?'}`")
        rb = au.get("rollback") or {}
        L.append("  - rollback: " + ("done" if a["rolled_back"] else ("available" if rb.get("supported") else "not supported")))
        if au.get("error"):
            L.append(f"  - error: {au['error']}")
    L.append("")

    note = repeat_note(incident, audits, prior_restarts)
    if note:
        L += [note, ""]
    elif int(incident.get("repeat_count") or 1) >= 2:
        L += [f"Repeat attempt {incident.get('repeat_count')}.", ""]

    L += ["---", f"Incident `{incident.get('incident_id')}`"]
    return "\n".join(L) + "\n"
