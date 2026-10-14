import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class CaptainJob:
    job_id: str
    captain: str
    command: str
    payload: Dict[str, Any]
    workspace_id: str
    org_id: Optional[str] = None
    user_id: Optional[str] = None
    queue: str = "engine-executions"
    status: str = "queued"
    session_id: Optional[str] = None
    result_summary: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    started_at: Optional[int] = None
    heartbeat_at: Optional[int] = None
    worker_id: Optional[str] = None
    task_id: Optional[str] = None
    lease_token: Optional[str] = None
    created_at: int = field(default_factory=_now_ms)
    updated_at: int = field(default_factory=_now_ms)
    events: List[Dict[str, Any]] = field(default_factory=list)

    def as_item(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "captain": self.captain,
            "command": self.command,
            "payload": self.payload,
            "workspace_id": self.workspace_id,
            "org_id": self.org_id,
            "user_id": self.user_id,
            "queue": self.queue,
            "status": self.status,
            "session_id": self.session_id,
            "result_summary": self.result_summary,
            "error": self.error,
            "started_at": self.started_at,
            "heartbeat_at": self.heartbeat_at,
            "worker_id": self.worker_id,
            "task_id": self.task_id,
            "lease_token": self.lease_token,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "events": self.events,
        }
