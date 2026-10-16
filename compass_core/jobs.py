from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

from compass_core.job_models import CaptainJob
from compass_core.local_state import local_state_store

logger = logging.getLogger(__name__)
TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_JOB_STATUSES = {"queued", "running", "retrying"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


RUNNING_JOB_STALE_AFTER_MS = max(
    15_000,
    _env_int(
        "CAPTAIN_RUNNING_JOB_STALE_AFTER_MS",
        _env_int(
            "ACTIVE_JOB_HEARTBEAT_TTL_SECONDS",
            45,
        )
        * 1000,
    ),
)
NON_RUNNING_JOB_STALE_AFTER_MS = max(
    60_000,
    _env_int("CAPTAIN_NON_RUNNING_JOB_STALE_AFTER_MS", 5 * 60 * 1000),
)


def _epoch_ms() -> int:
    return int(time.time() * 1000)

def _job_event(
    message: str,
    *,
    event_type: str = "info",
    status: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "timestamp": _epoch_ms(),
        "message": message,
        "type": event_type,
    }
    if status:
        event["status"] = status
    if data:
        event["data"] = data
    return event


class CaptainJobStore:
    """Persist captain job metadata to DynamoDB or local fallback."""

    def __init__(
        self,
        table_name: Optional[str] = None,
        region_name: Optional[str] = None,
        *,
        job_id_index: Optional[str] = None,
    ) -> None:
        self.table_name = table_name or os.getenv("CAPTAIN_JOBS_TABLE")
        self.region_name = (
            region_name
            or os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
        )
        self._multi_tenant = str(os.getenv("CAPTAIN_JOBS_MULTI_TENANT", "1")).lower() not in {"0", "false", "no"}
        self._pk_name = os.getenv("CAPTAIN_JOBS_PARTITION_KEY", "pk")
        self._sk_name = os.getenv("CAPTAIN_JOBS_SORT_KEY", "sk")
        self._job_id_index = job_id_index if job_id_index is not None else os.getenv("CAPTAIN_JOBS_JOB_ID_INDEX", "")
        self._table = None
        if self.table_name:
            try:
                resource = (
                    boto3.resource("dynamodb", region_name=self.region_name)
                    if self.region_name
                    else boto3.resource("dynamodb")
                )
                self._table = resource.Table(self.table_name)
            except (BotoCoreError, ClientError) as exc:
                logger.warning(
                    "Falling back to local job store; DynamoDB table %s unavailable: %s",
                    self.table_name,
                    exc,
                )
                self._table = None

    # -- private helpers -------------------------------------------------
    def _workspace_partition(self, workspace_id: str) -> str:
        return f"WORKSPACE#{workspace_id}"

    def _job_sort_key(self, job_id: str) -> str:
        return f"JOB#{job_id}"

    def _key(self, job_id: str, workspace_id: Optional[str]) -> Dict[str, Any]:
        if self._multi_tenant and workspace_id:
            return {
                self._pk_name: self._workspace_partition(workspace_id),
                self._sk_name: self._job_sort_key(job_id),
            }
        return {"job_id": job_id}

    def _ensure_storage_keys(self, item: Dict[str, Any]) -> Dict[str, Any]:
        if not self._multi_tenant:
            return item
        workspace_id = item.get("workspace_id")
        if not workspace_id:
            return item
        enriched = dict(item)
        enriched[self._pk_name] = self._workspace_partition(str(workspace_id))
        enriched[self._sk_name] = self._job_sort_key(str(item["job_id"]))
        return enriched

    def _persist_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        payload = self._ensure_storage_keys(item)
        if self._table:
            try:
                self._table.put_item(Item=payload)
                return payload
            except (BotoCoreError, ClientError) as exc:
                logger.warning(
                    "DynamoDB write failed for job %s; switching to local store: %s",
                    item.get("job_id"),
                    exc,
                )
                self._table = None
        return local_state_store.create_job(item["job_id"], payload)

    def _load_job(self, job_id: str, workspace_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if self._table:
            try:
                response = self._table.get_item(Key=self._key(job_id, workspace_id))
                item = response.get("Item")
                if item:
                    return item
                if self._multi_tenant and not workspace_id:
                    if self._job_id_index:
                        lookup = self._table.query(
                            IndexName=self._job_id_index,
                            KeyConditionExpression=Key("job_id").eq(job_id),
                            Limit=1,
                        )
                        gsi_items = lookup.get("Items") or []
                        if gsi_items:
                            return gsi_items[0]
                    else:
                        lookup = self._table.scan(FilterExpression=Attr("job_id").eq(job_id), Limit=1)
                        scan_items = lookup.get("Items") or []
                        if scan_items:
                            return scan_items[0]
            except (BotoCoreError, ClientError) as exc:
                logger.warning(
                    "DynamoDB get_item failed for %s; falling back to local store: %s",
                    job_id,
                    exc,
                )
                self._table = None
        local_job = local_state_store.get_job(job_id)
        if not local_job:
            return None
        if workspace_id and local_job.get("workspace_id") not in (None, workspace_id):
            return None
        return local_job

    def _scan_jobs(self, limit: int, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if self._table:
            try:
                # Keys are JOB#<uuid>: key order is not time order, so every page is read
                # and the newest are taken after sorting. One page of `limit` items lost the
                # newest jobs once a workspace had more than `limit` of them.
                if self._multi_tenant and workspace_id:
                    kwargs: Dict[str, Any] = {"KeyConditionExpression": Key(self._pk_name).eq(self._workspace_partition(workspace_id))}
                    read = self._table.query
                else:
                    kwargs = {}
                    read = self._table.scan
                items: List[Dict[str, Any]] = []
                while True:
                    response = read(**kwargs)
                    items.extend(response.get("Items", []))
                    if not response.get("LastEvaluatedKey"):
                        break
                    kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                items.sort(key=lambda entry: entry.get("updated_at", entry.get("created_at", 0)), reverse=True)
                return items[:limit]
            except (BotoCoreError, ClientError) as exc:
                logger.warning(
                    "DynamoDB scan failed; reverting to local job list: %s",
                    exc,
                )
                self._table = None
        jobs = local_state_store.list_jobs(limit * 2 if workspace_id else limit)
        if workspace_id:
            jobs = [job for job in jobs if job.get("workspace_id") == workspace_id]
        return jobs[:limit]

    def _merge_update(
        self,
        job_id: str,
        updates: Dict[str, Any],
        *,
        workspace_id: Optional[str] = None,
        event: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        existing = self._load_job(job_id, workspace_id=workspace_id)
        if not existing:
            return None
        merged = dict(existing)
        merged.update(updates)
        merged["job_id"] = job_id
        if event:
            events = list(existing.get("events") or [])
            events.append(event)
            merged["events"] = events
            merged["updated_at"] = event.get("timestamp", _epoch_ms())
        else:
            merged["updated_at"] = _epoch_ms()
        return self._persist_item(merged)

    def _is_terminal(self, item: Optional[Dict[str, Any]]) -> bool:
        status = str((item or {}).get("status") or "").lower()
        return status in TERMINAL_JOB_STATUSES

    def _lease_is_stale(self, item: Dict[str, Any], stale_after_ms: int) -> bool:
        heartbeat_at = item.get("heartbeat_at") or item.get("updated_at") or item.get("started_at") or 0
        try:
            heartbeat_ms = int(heartbeat_at)
        except (TypeError, ValueError):
            heartbeat_ms = 0
        return heartbeat_ms <= 0 or (_epoch_ms() - heartbeat_ms) > stale_after_ms

    def _activity_at_ms(self, item: Dict[str, Any]) -> int:
        for field in ("heartbeat_at", "updated_at", "started_at", "created_at"):
            try:
                value = int(item.get(field) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
        return 0

    def _stale_failure_reason(self, item: Dict[str, Any]) -> Optional[str]:
        status = str(item.get("status") or "").lower()
        if status == "running":
            if self._lease_is_stale(item, RUNNING_JOB_STALE_AFTER_MS):
                return "Job interrupted after worker heartbeat stopped."
            return None
        if status not in {"queued", "retrying"}:
            return None
        activity_at = self._activity_at_ms(item)
        if activity_at <= 0:
            return None
        if (_epoch_ms() - activity_at) <= NON_RUNNING_JOB_STALE_AFTER_MS:
            return None
        if status == "retrying":
            return "Job retry stalled before a worker could resume it."
        return "Job stalled before a worker could start."

    def _repair_stale_job(self, item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not item or self._is_terminal(item):
            return item
        reason = self._stale_failure_reason(item)
        if not reason:
            return item
        repaired = dict(item)
        repaired["status"] = "failed"
        repaired["error"] = reason
        repaired["heartbeat_at"] = None
        repaired["worker_id"] = None
        repaired["task_id"] = None
        repaired["lease_token"] = None
        event = _job_event(
            "Job failed",
            event_type="status",
            status="failed",
            data={"error": reason},
        )
        events = list(repaired.get("events") or [])
        events.append(event)
        repaired["events"] = events
        repaired["updated_at"] = event["timestamp"]
        return self._persist_item(repaired)

    # -- public API ------------------------------------------------------
    def create_job(
        self,
        *,
        captain: str,
        command: str,
        payload: Dict[str, Any],
        queue: str,
        workspace_id: str,
        org_id: Optional[str] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        job = CaptainJob(
            job_id=job_id or str(uuid.uuid4()),
            captain=captain,
            command=command,
            payload=payload,
            workspace_id=workspace_id,
            org_id=org_id,
            user_id=user_id,
            queue=queue,
            session_id=session_id,
        )
        job.events.append(
            _job_event(
                f"Job queued on {queue}",
                event_type="status",
                status="queued",
            )
        )
        item = job.as_item()
        return self._persist_item(item)

    def claim_running(
        self,
        job_id: str,
        *,
        workspace_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        task_id: Optional[str] = None,
        lease_token: Optional[str] = None,
        stale_after_ms: int = 180000,
    ) -> Optional[Dict[str, Any]]:
        existing = self._load_job(job_id, workspace_id=workspace_id)
        if not existing:
            return None
        if self._is_terminal(existing):
            logger.info(
                "Ignoring redelivery for terminal job %s with status %s",
                job_id,
                existing.get("status"),
            )
            return None

        event_message = "Worker started processing job"
        current_status = str(existing.get("status") or "").lower()
        if current_status == "running":
            current_lease = existing.get("lease_token")
            if current_lease and lease_token and current_lease == lease_token:
                return existing
            if not self._lease_is_stale(existing, stale_after_ms):
                logger.info(
                    "Ignoring concurrent delivery for active job %s owned by %s",
                    job_id,
                    existing.get("worker_id") or "unknown-worker",
                )
                return None
            event_message = "Recovered stale running job after heartbeat timeout"

        now = _epoch_ms()
        updates = {
            "status": "running",
            "error": None,
            "started_at": existing.get("started_at") or now,
            "heartbeat_at": now,
            "worker_id": worker_id,
            "task_id": task_id,
            "lease_token": lease_token,
        }
        return self._merge_update(
            job_id,
            updates,
            workspace_id=workspace_id,
            event=_job_event(
                event_message,
                event_type="status",
                status="running",
                data={"worker_id": worker_id, "task_id": task_id} if worker_id or task_id else None,
            ),
        )

    def touch_heartbeat(
        self,
        job_id: str,
        *,
        workspace_id: Optional[str] = None,
        lease_token: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        existing = self._load_job(job_id, workspace_id=workspace_id)
        if not existing:
            return None
        if str(existing.get("status") or "").lower() != "running":
            return None
        current_lease = existing.get("lease_token")
        if lease_token and current_lease and current_lease != lease_token:
            return None
        return self._merge_update(
            job_id,
            {"heartbeat_at": _epoch_ms()},
            workspace_id=workspace_id,
        )

    def mark_completed(
        self,
        job_id: str,
        result_summary: Optional[Dict[str, Any]] = None,
        *,
        workspace_id: Optional[str] = None,
        lease_token: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        existing = self._load_job(job_id, workspace_id=workspace_id)
        if not existing:
            return None
        current_lease = existing.get("lease_token")
        if lease_token and current_lease and current_lease != lease_token:
            logger.info("Ignoring completion for job %s because lease ownership changed", job_id)
            return None
        updates: Dict[str, Any] = {
            "status": "completed",
            "result_summary": result_summary,
            "error": None,
            "heartbeat_at": None,
            "worker_id": None,
            "task_id": None,
            "lease_token": None,
        }
        return self._merge_update(
            job_id,
            updates,
            workspace_id=workspace_id,
            event=_job_event(
                "Job completed successfully",
                event_type="status",
                status="completed",
                data={"result_summary": result_summary} if result_summary else None,
            ),
        )

    def mark_failed(
        self,
        job_id: str,
        error: str,
        result_summary: Optional[Dict[str, Any]] = None,
        *,
        workspace_id: Optional[str] = None,
        lease_token: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        existing = self._load_job(job_id, workspace_id=workspace_id)
        if not existing:
            return None
        current_lease = existing.get("lease_token")
        if lease_token and current_lease and current_lease != lease_token:
            logger.info("Ignoring failure update for job %s because lease ownership changed", job_id)
            return None
        updates = {
            "status": "failed",
            "error": error,
            "result_summary": result_summary,
            "heartbeat_at": None,
            "worker_id": None,
            "task_id": None,
            "lease_token": None,
        }
        return self._merge_update(
            job_id,
            updates,
            workspace_id=workspace_id,
            event=_job_event(
                "Job failed",
                event_type="status",
                status="failed",
                data={"error": error},
            ),
        )

    def mark_retrying(
        self,
        job_id: str,
        error: str,
        *,
        workspace_id: Optional[str] = None,
        lease_token: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        existing = self._load_job(job_id, workspace_id=workspace_id)
        if not existing:
            return None
        current_lease = existing.get("lease_token")
        if lease_token and current_lease and current_lease != lease_token:
            logger.info("Ignoring retry update for job %s because lease ownership changed", job_id)
            return None
        updates = {
            "status": "retrying",
            "error": error,
            "heartbeat_at": None,
            "worker_id": None,
            "task_id": None,
            "lease_token": None,
        }
        return self._merge_update(
            job_id,
            updates,
            workspace_id=workspace_id,
            event=_job_event(
                "Retry scheduled after failure",
                event_type="status",
                status="retrying",
                data={"error": error},
            ),
        )

    def get_job(self, job_id: str, workspace_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return self._repair_stale_job(self._load_job(job_id, workspace_id=workspace_id))

    def list_jobs(self, limit: int = 100, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        jobs = self._scan_jobs(limit, workspace_id=workspace_id)
        repaired = [self._repair_stale_job(job) or job for job in jobs]
        repaired.sort(key=lambda entry: entry.get("updated_at", entry.get("created_at", 0)), reverse=True)
        return repaired[:limit]

    def find_active_job(
        self,
        *,
        session_id: str,
        workspace_id: str,
        captain: Optional[str] = None,
        command: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        if not session_id or not workspace_id:
            return None
        lookback = limit
        if lookback is None:
            lookback = max(100, _env_int("CAPTAIN_ACTIVE_SESSION_LOOKBACK", 500))

        jobs = self._scan_jobs(int(lookback), workspace_id=workspace_id)
        for job in jobs:
            if str(job.get("session_id") or "") != session_id:
                continue
            if captain and str(job.get("captain") or "") != captain:
                continue
            if command and str(job.get("command") or "") != command:
                continue
            repaired = self._repair_stale_job(job) or job
            status = str(repaired.get("status") or "").lower()
            if status in ACTIVE_JOB_STATUSES:
                return repaired
        return None

    def append_event(
        self,
        job_id: str,
        message: str,
        *,
        event_type: str = "info",
        status: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        event = _job_event(
            message,
            event_type=event_type,
            status=status,
            data=data,
        )
        return self._merge_update(job_id, {}, workspace_id=workspace_id, event=event)

    def reap_stale_jobs(
        self,
        *,
        stale_after_ms: int,
        limit: int = 100,
        reason: str = "Job interrupted after worker heartbeat stopped.",
    ) -> List[str]:
        stale_job_ids: List[str] = []
        jobs = self._scan_jobs(limit * 2)
        for job in jobs:
            if str(job.get("status") or "").lower() != "running":
                continue
            if not self._lease_is_stale(job, stale_after_ms):
                continue
            workspace_id = job.get("workspace_id")
            job_id = job.get("job_id")
            if not workspace_id or not job_id:
                continue
            updated = self.mark_failed(
                str(job_id),
                reason,
                workspace_id=str(workspace_id),
                lease_token=job.get("lease_token"),
            )
            if updated:
                stale_job_ids.append(str(job_id))
            if len(stale_job_ids) >= limit:
                break
        return stale_job_ids


JOB_ID_INDEX = "JobIdIndex"


def store_for(config: Any) -> Optional["CaptainJobStore"]:
    """The job store named by a workspace config (`dynamodb_config.jobs`), or None."""
    from compass_core.tables import tables

    table = tables(config).jobs
    if not table:
        return None
    return CaptainJobStore(table_name=table, job_id_index=JOB_ID_INDEX)


HEARTBEAT_SECONDS = 30.0


class heartbeat:
    """Keep a running job's heartbeat fresh while the block runs.

    The store treats a running job with no heartbeat for three minutes as dead
    (``_repair_stale_job``); a map scan takes longer than that. The product's
    Celery task beats from its own loop; this is the same for jobs run in-process.
    """

    def __init__(self, store: Any, job_id: str, *, workspace_id: Optional[str] = None, interval: float = HEARTBEAT_SECONDS) -> None:
        import threading

        self._store = store
        self._job_id = job_id
        self._workspace_id = workspace_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"heartbeat:{job_id[:8]}", daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._store.touch_heartbeat(self._job_id, workspace_id=self._workspace_id)
            except Exception as exc:  # noqa: BLE001 - a missed beat is not worth failing the job over
                logger.debug("heartbeat for %s failed: %s", self._job_id, exc)

    def __enter__(self) -> "heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
