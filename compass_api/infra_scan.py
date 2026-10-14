from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from compass_api import workspace as api_workspace
from compass_core.aws import has_target_aws_access
from compass_core.queue import job_queue

ACTIVE_MAPPER_JOB_STATUSES = {"queued", "running", "retrying"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_scan_state(config: Dict[str, Any]) -> Dict[str, Any]:
    state = config.get("infra_scan")
    if not isinstance(state, dict):
        state = {}
        config["infra_scan"] = state
    return state


def aws_integration_configured(config: Any) -> bool:
    return has_target_aws_access(config, capability="read")


def github_integration_configured(config: Any) -> bool:
    if not isinstance(config, dict):
        return False
    github_cfg = config.get("github")
    if not isinstance(github_cfg, dict):
        return False
    installation_id = github_cfg.get("installation_id")
    token = github_cfg.get("token")
    return installation_id is not None or bool(str(token or "").strip())


def infra_scan_integrations_ready(config: Any) -> bool:
    return aws_integration_configured(config) and github_integration_configured(config)


def active_mapper_job(
    workspace_id: Optional[str],
    *,
    queue_client: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    if not workspace_id:
        return None
    client = queue_client or job_queue()
    jobs = client.list_jobs(limit=100, workspace_id=workspace_id)
    for job in jobs:
        status = str(job.get("status") or "").lower()
        if (
            job.get("captain") == "mapper"
            and job.get("command") == "run"
            and status in ACTIVE_MAPPER_JOB_STATUSES
        ):
            return job
    return None


def mark_infra_scan_pending(
    config: Dict[str, Any],
    *,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    state = _workspace_scan_state(config)
    now = _now_iso()
    state["scanned"] = False
    state["updated_at"] = now
    state["last_requested_at"] = now
    if reason:
        state["last_reset_reason"] = reason
    state.pop("last_error", None)
    state.pop("last_failed_at", None)
    return state


def mark_infra_scan_enqueued(
    config: Dict[str, Any],
    *,
    job_id: Optional[str],
    trigger: str,
) -> Dict[str, Any]:
    state = mark_infra_scan_pending(config)
    now = _now_iso()
    state["last_enqueued_at"] = now
    state["last_trigger"] = trigger
    state["updated_at"] = now
    if job_id:
        state["last_job_id"] = job_id
    return state


def mark_infra_scan_completed(
    config: Dict[str, Any],
    *,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    state = _workspace_scan_state(config)
    now = _now_iso()
    state["scanned"] = True
    state["updated_at"] = now
    state["last_completed_at"] = now
    if job_id:
        state["last_job_id"] = job_id
    state.pop("last_error", None)
    state.pop("last_failed_at", None)
    return state


def mark_infra_scan_failed(
    config: Dict[str, Any],
    *,
    error: Optional[str],
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    state = _workspace_scan_state(config)
    now = _now_iso()
    state["scanned"] = False
    state["updated_at"] = now
    state["last_failed_at"] = now
    if job_id:
        state["last_job_id"] = job_id
    if error:
        state["last_error"] = str(error).strip()
    return state


def summarize_infra_scan(
    config: Dict[str, Any],
    *,
    active_job_record: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    state = _workspace_scan_state(config)
    scanned = bool(state.get("scanned"))
    integrations_ready = infra_scan_integrations_ready(config)
    active_job = active_job_record
    active_status = str((active_job or {}).get("status") or "").lower()
    latest_message = None
    latest_event = None
    events = active_job.get("events") if isinstance(active_job, dict) else None
    if isinstance(events, list) and events:
        latest_event = events[-1]
        latest_message = latest_event.get("message") if isinstance(latest_event, dict) else None

    if active_status in ACTIVE_MAPPER_JOB_STATUSES:
        status = active_status
    elif scanned:
        status = "completed"
    elif not integrations_ready:
        status = "awaiting_integrations"
    elif state.get("last_error"):
        status = "failed"
    else:
        status = "pending"

    disabled_reason: Optional[str]
    if scanned:
        disabled_reason = None
    elif not integrations_ready:
        disabled_reason = "Connect AWS and GitHub, then complete the first infra scan before Beamreach can run triage or fixes."
    elif status in ACTIVE_MAPPER_JOB_STATUSES:
        disabled_reason = "Infra scan is in progress. Triage and fix unlock when the first full scan completes."
    elif status == "failed":
        disabled_reason = "Infra scan has not completed successfully yet. Retry the full scan before running triage or fixes."
    else:
        disabled_reason = "Beamreach requires the first full infra scan before triage and fix are available."

    return {
        "scanned": scanned,
        "integrations_ready": integrations_ready,
        "status": status,
        "job_id": (active_job or {}).get("job_id") or state.get("last_job_id"),
        "job_status": (active_job or {}).get("status"),
        "latest_message": latest_message,
        "latest_event": latest_event,
        "last_requested_at": state.get("last_requested_at"),
        "last_enqueued_at": state.get("last_enqueued_at"),
        "last_completed_at": state.get("last_completed_at"),
        "last_failed_at": state.get("last_failed_at"),
        "last_error": state.get("last_error"),
        "last_trigger": state.get("last_trigger"),
        "disabled_reason": disabled_reason,
    }


def persist_infra_scan_update(
    workspace_id: str,
    *,
    org_id: Optional[str] = None,
    user_id: Optional[str] = None,
    updater: Callable[[Dict[str, Any]], None],
) -> Dict[str, Any]:
    ctx = api_workspace.load(
        workspace_id,
        org_id=org_id,
        user_id=user_id,
        force_refresh=True,
    )
    config = dict(ctx.config or {})
    updater(config)
    api_workspace.save(ctx, config)
    return config
