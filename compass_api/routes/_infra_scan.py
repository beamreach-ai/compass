from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException, Request

from compass_api.infra_scan import (
    active_mapper_job,
    mark_infra_scan_enqueued,
    mark_infra_scan_pending,
    summarize_infra_scan,
)
from compass_api.routes._workspace import (
    get_actor_ids,
    get_workspace_config,
    get_workspace_context,
    get_workspace_target,
    save_config,
)
from compass_core.queue import queue_proxy

queue_client = queue_proxy


def _mapper_org_id(request: Request, config: Dict[str, Any], *, org_id_override: Optional[str] = None) -> Optional[str]:
    actor_ids = get_actor_ids(request)
    workspace_ctx = get_workspace_context(request)
    return (
        org_id_override
        or actor_ids["org_id"]
        or workspace_ctx.org_id
    )


def current_infra_scan_summary(request: Request, *, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    actor_ids = get_actor_ids(request)
    workspace_config = config if isinstance(config, dict) else get_workspace_config(request)
    mapper_job = active_mapper_job(actor_ids["workspace_id"], queue_client=queue_client)
    return summarize_infra_scan(workspace_config, active_job_record=mapper_job)


def mark_infra_scan_pending_for_request(
    request: Request,
    *,
    config: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    workspace_config = config if isinstance(config, dict) else get_workspace_config(request)
    mark_infra_scan_pending(workspace_config, reason=reason)
    save_config(request, workspace_config)
    return workspace_config


def enqueue_full_infra_scan(
    request: Request,
    *,
    config: Optional[Dict[str, Any]] = None,
    trigger: str,
    org_id_override: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    workspace_config = config if isinstance(config, dict) else get_workspace_config(request)
    summary = current_infra_scan_summary(request, config=workspace_config)
    if not summary.get("integrations_ready"):
        return None

    actor_ids = get_actor_ids(request)
    active = active_mapper_job(actor_ids["workspace_id"], queue_client=queue_client)
    if active:
        existing_job = dict(active)
        existing_job["existing"] = True
        return existing_job

    target = get_workspace_target(request)
    job = queue_client.enqueue(
        captain="mapper",
        command="run",
        payload={
            "target": target,
            "workspace_id": actor_ids["workspace_id"],
            "org_id": _mapper_org_id(request, workspace_config, org_id_override=org_id_override),
            "user_id": actor_ids["user_id"],
            "mode": "full",
        },
    )
    mark_infra_scan_enqueued(
        workspace_config,
        job_id=str(job.get("job_id") or ""),
        trigger=trigger,
    )
    save_config(request, workspace_config)
    job["existing"] = False
    return job


def ensure_infra_scan_completed(
    request: Request,
    *,
    action_label: str = "run triage or fixes",
) -> Dict[str, Any]:
    summary = current_infra_scan_summary(request)
    if summary.get("scanned") is True:
        return summary
    detail = summary.get("disabled_reason") or f"Complete the infra scan before Beamreach can {action_label}."
    raise HTTPException(status_code=409, detail=detail)
