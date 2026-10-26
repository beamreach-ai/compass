"""The decoy captain's inbox items (docs/specs/one-inbox.md §3.3): a response the gate
parked for approval, a decoy whose pull request waits for a merge, and one whose pull
request merged but that is not armed yet. ``items`` is what the manifest names."""

from __future__ import annotations

from typing import Any, Dict, List

from captains.honeypot import incidents
from compass_core.inbox import Action, Context, Item, _ms, _query
from compass_core.keys import honeytoken_prefix


def items(ctx: Context) -> List[Item]:
    if ctx.table is None:
        return []
    return parked_responses(ctx) + waiting_decoys(ctx)


def parked_responses(ctx: Context) -> List[Item]:
    """Each parked response on an open incident: the gate said ``wait_for_approval``."""
    out: List[Item] = []
    for incident in incidents.list_incidents(ctx.table, ctx.pk, limit=50):
        if str(incident.get("status") or "open") != "open":
            continue
        action = incident.get("action") if isinstance(incident.get("action"), dict) else {}
        pending = [p for p in (action.get("pending") or []) if isinstance(p, dict) and p.get("action_id")]
        if not pending:
            continue
        incident_id = str(incident.get("incident_id"))
        context = action.get("context") if isinstance(action.get("context"), dict) else {}
        since = _ms(incident.get("action_started_at")) or incidents.incident_created_ms(incident)
        token = (incident.get("honeytoken_ids") or [None])[0]
        for entry in pending:
            risk = int(entry.get("risk") or 0)
            action_type = str(entry.get("action_type") or "")
            target = context.get("service_arn") or context.get("task_arn") or context.get("user_arn") or incident.get("service_arn") or ""
            target_name = str(target).rsplit("/", 1)[-1] if target else "the target"
            plan = next((p for p in (action.get("plan") or []) if isinstance(p, dict) and p.get("action_id") == entry.get("action_id")), {})
            out.append(Item(
                id=f"decoy.approval:{incident_id}:{action_type}", kind="decoy.approval", verb="approve",
                severity="urgent" if risk >= 3 else "normal", captain="honeypot", tab="honeypots",
                title=f"{action_type.replace('_', ' ').capitalize()} on {target_name} for incident {incident_id[-6:]}",
                summary=f"risk {risk} · chosen by the agent · parked: {entry.get('reason') or 'waiting for approval'}",
                since=since, subject={"incident_id": incident_id, "action_id": entry.get("action_id"), "action_type": action_type},
                actions=[
                    Action(key="run", label="Run", method="POST", path=f"/api/honeypots/incidents/{incident_id}/approve",
                           body={"action_id": entry.get("action_id")}, scope="operate", confirm=f"Runs {action_type} on {target_name} (risk {risk})."),
                    Action(key="decline", label="Decline", method="POST", path=f"/api/honeypots/incidents/{incident_id}/decline",
                           body={"action_id": entry.get("action_id")}, scope="operate"),
                    Action(key="open", label="Open incident", href=f"/workspace?tab=honeypots&view=history&incident={incident_id}"),
                ],
                detail={"risk": {"score": risk, "breakdown": plan.get("breakdown") or plan.get("risk_breakdown"), "notes": plan.get("notes")},
                        "reason": entry.get("reason"), "context": context,
                        "incident": {"opened_at": incident.get("first_event_time"), "honeytoken": token, "source_ip": incident.get("source_ip"),
                                     "alert_count": incident.get("alert_count")}},
            ))
    return out


def waiting_decoys(ctx: Context) -> List[Item]:
    """Decoys whose plant or decommission pull request is open (``merge``), and those whose
    plant pull request merged and that are not armed yet (``fix``)."""
    tokens = _query(ctx.table, ctx.pk, honeytoken_prefix())
    if not tokens:
        return []
    by_token: Dict[str, List[Dict[str, Any]]] = {}
    for record in ctx.pr_records():
        token_id = (record.get("origin") or {}).get("honeytoken_id")
        if token_id and str(record.get("type") or "").startswith("honeypot_"):
            by_token.setdefault(str(token_id), []).append(record)
    out: List[Item] = []
    for token in tokens:
        token_id = str(token.get("honeytoken_id") or token.get("asset_id") or "")
        status = str(token.get("status") or "")
        name = str(token.get("name") or token_id)
        token_class = token.get("honeytoken_class") or token.get("asset_class")
        placement = token.get("placement") if isinstance(token.get("placement"), dict) else {}
        records = sorted(by_token.get(token_id, []), key=lambda r: int(r.get("created_at") or 0), reverse=True)
        latest = records[0] if records else None
        pr_url = (latest or {}).get("url") or placement.get("pr_url")
        if status in ("pending_pr", "pending_merge") and (latest is None or str(latest.get("state") or "open") == "open"):
            out.append(Item(
                id=f"decoy.waiting:{token_id}", kind="decoy.waiting", verb="merge", severity="info", captain="honeypot", tab="honeypots",
                title=f"Merge the pull request that plants {name}",
                summary=f"{token_class or 'decoy'} · {placement.get('module_path') or placement.get('repo') or 'no placement'}",
                since=_ms((latest or {}).get("created_at") or token.get("created_at")), subject={"honeytoken_id": token_id},
                actions=[a for a in (
                    Action(key="open", label="Open pull request", href=pr_url) if pr_url else None,
                    Action(key="token", label="Open decoy", href=f"/workspace?tab=honeypots&token={token_id}"),
                ) if a is not None],
                detail={"class": token_class, "placement": placement, "pull_request": pr_url},
            ))
        elif status not in ("armed", "active", "decommissioned") and latest is not None and str(latest.get("state")) == "merged" \
                and str(latest.get("type")) == "honeypot_plant":
            out.append(Item(
                id=f"decoy.arm:{token_id}", kind="decoy.arm", verb="fix", severity="normal", captain="honeypot", tab="honeypots",
                title=f"Arm {name}: its pull request merged",
                summary="merged, not armed yet · run Arm once the Terraform is applied",
                since=_ms(latest.get("updated_at") or latest.get("created_at")), subject={"honeytoken_id": token_id},
                actions=[
                    Action(key="arm", label="Arm", method="POST", path=f"/api/honeypots/tokens/{token_id}/arm", body={}, scope="operate"),
                    Action(key="token", label="Open decoy", href=f"/workspace?tab=honeypots&token={token_id}"),
                ],
                detail={"class": token_class, "placement": placement, "last_arm_checks": token.get("arm_checks") or token.get("checks"), "pull_request": pr_url},
            ))
    return out
