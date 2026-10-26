from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from compass_api import extensions
from compass_api.routes._app_config import ensure_feature_enabled, ensure_not_read_only
from compass_api.routes._workspace import get_actor_ids
from compass_core.queue import queue_proxy

router = APIRouter()
queue_client = queue_proxy


class JobEvent(BaseModel):
    timestamp: int = Field(..., description="Epoch milliseconds when the event occurred")
    message: str
    type: str = Field(default="info")
    status: Optional[str] = None
    data: Optional[Dict[str, Any]] = None


class JobSummary(BaseModel):
    job_id: str
    captain: str
    command: str
    workspace_id: Optional[str] = None
    org_id: Optional[str] = None
    user_id: Optional[str] = None
    status: str
    queue: str = Field(default="captain-executions")
    session_id: Optional[str] = None
    result_summary: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: int = Field(..., description="Epoch milliseconds when the job was created")
    updated_at: int = Field(..., description="Epoch milliseconds when the job was last updated")
    events: List[JobEvent] = Field(default_factory=list)


def _native(value: Any) -> Any:
    """The table's Decimals as the numbers they were: pydantic would write them as strings."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_native(v) for v in value]
    return value


def _summary(job: Dict[str, Any]) -> JobSummary:
    """A job record as the API shows it; an inline run (no jobs table) lacks the
    timestamps a stored record has."""
    import time

    now = int(time.time() * 1000)
    record = {"queue": "inline", "created_at": now, "updated_at": now, "events": [], **{k: v for k, v in job.items() if v is not None or k in ("error",)}}
    return JobSummary(**_native(record))


class StartJobPayload(BaseModel):
    captain: str
    command: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    session_id: Optional[str] = None


@router.post("/api/jobs", response_model=JobSummary)
def start_job(body: StartJobPayload, request: Request) -> JobSummary:
    """Start one of a captain's commands: the generic way to drive a captain that has
    no routes of its own (a third-party captain, the Captains page's Run button).
    The queue refuses a captain that is off, with the reason."""
    ensure_feature_enabled("queue", detail="Queue is disabled in this environment.")
    ensure_not_read_only()
    from compass_core import captains as registry

    known = registry.manifest(body.captain)
    if known is None:
        raise HTTPException(status_code=404, detail=f"no captain named {body.captain!r} is installed")
    if body.command not in registry.commands_of(body.captain):
        raise HTTPException(status_code=404, detail=f"{known.label} has no command {body.command!r}")
    actor_ids = get_actor_ids(request)
    job = queue_client.enqueue(
        captain=body.captain,
        command=body.command,
        payload={**body.payload, **{k: v for k, v in actor_ids.items() if v}},
        session_id=body.session_id,
    )
    return _summary(job)


@router.get("/api/jobs", response_model=List[JobSummary])
def list_jobs(request: Request, limit: int = Query(default=50, ge=1, le=500)) -> List[JobSummary]:
    ensure_feature_enabled("queue", detail="Queue is disabled in this environment.")
    actor_ids = get_actor_ids(request)
    jobs = queue_client.list_jobs(limit=limit, workspace_id=actor_ids["workspace_id"])
    return [JobSummary(**_native(job)) for job in jobs]


@router.get("/api/jobs/{job_id}", response_model=JobSummary)
def get_job(job_id: str, request: Request) -> JobSummary:
    ensure_feature_enabled("queue", detail="Queue is disabled in this environment.")
    actor_ids = get_actor_ids(request)
    job = queue_client.get_job(job_id, workspace_id=actor_ids["workspace_id"])
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobSummary(**_native(job))


@router.post("/api/jobs/{job_id}/restart", response_model=JobSummary)
def restart_job(job_id: str, request: Request) -> JobSummary:
    ensure_feature_enabled("queue", detail="Queue is disabled in this environment.")
    ensure_not_read_only()
    actor_ids = get_actor_ids(request)
    job = queue_client.get_job(job_id, workspace_id=actor_ids["workspace_id"])
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    extensions.run_job_restart_guards(request, job)

    payload = job.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    session_id = job.get("session_id")
    queue_name = job.get("queue")

    new_job = queue_client.enqueue(
        captain=job["captain"],
        command=job["command"],
        payload={
            **payload,
            "workspace_id": actor_ids["workspace_id"],
            "org_id": actor_ids["org_id"],
            "user_id": actor_ids["user_id"],
        },
        session_id=session_id,
        queue=queue_name,
    )
    return _summary(new_job)
