"""Where follow-up jobs go.

A captain that needs another job run (the decoys' alert → investigate →
respond chain, the PR watcher's sync → address) asks ``job_queue()`` and calls
``enqueue``. The open default, ``InlineQueue``, runs the job right there in
the calling process with no broker; it needs a dispatch function that maps
``(captain, command, payload)`` to a call. The product installs its Celery
client with ``set_job_queue``.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import Any, Callable, Dict, List, Optional, Protocol
from uuid import uuid4

from compass_core.events import job_event_context
from compass_core.jobs import heartbeat

logger = logging.getLogger("compass.queue")

Dispatch = Callable[[str, str, Dict[str, Any]], Any]


def check_enqueue(captain: str, payload: Any) -> None:
    """Refuse a captain the payload's workspace cannot run (compass_core.captains);
    every queue implementation calls this first, so the refusal comes before a
    job record exists and with the reason."""
    from compass_core import captains as _captains

    _captains.ensure_active_for_payload(captain, payload if isinstance(payload, dict) else {})


class JobQueue(Protocol):
    def enqueue(
        self,
        *,
        captain: str,
        command: str,
        payload: Dict[str, Any],
        session_id: Optional[str] = None,
        queue: Optional[str] = None,
        enforce_single_active_session: bool = False,
    ) -> Dict[str, Any]: ...

    def get_job(self, job_id: str, workspace_id: Optional[str] = None) -> Optional[Dict[str, Any]]: ...


class InlineQueue:
    """Runs each job synchronously, in the caller's process, inside a job event
    context. Records go to a job store when one is given (``store`` or a
    ``store_factory`` resolved on first use), else they are kept in memory. A
    job that raises is recorded as failed and the exception propagates."""

    def __init__(
        self,
        dispatch: Optional[Dispatch] = None,
        *,
        store: Any = None,
        store_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._dispatch = dispatch
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._store = store
        self._store_factory = store_factory
        self._store_resolved = store is not None

    @property
    def store(self) -> Any:
        if not self._store_resolved:
            self._store_resolved = True
            if self._store_factory is not None:
                try:
                    self._store = self._store_factory()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("no job store: %s", exc)
        return self._store

    def enqueue(
        self,
        *,
        captain: str,
        command: str,
        payload: Dict[str, Any],
        session_id: Optional[str] = None,
        queue: Optional[str] = None,
        enforce_single_active_session: bool = False,
    ) -> Dict[str, Any]:
        if self._dispatch is None:
            raise RuntimeError(
                "no job queue is configured: call compass_core.queue.set_job_queue(...) "
                "or give InlineQueue a dispatch function"
            )
        check_enqueue(captain, payload)
        workspace_id = str(payload.get("workspace_id")) if isinstance(payload, dict) and payload.get("workspace_id") else None
        store = self.store
        if store is not None:
            created = store.create_job(
                captain=captain, command=command, payload=dict(payload), queue=queue or "inline",
                workspace_id=workspace_id or "default", org_id=payload.get("org_id"), user_id=payload.get("user_id"), session_id=session_id,
            )
            job_id = str(created["job_id"])
            store.claim_running(job_id, workspace_id=workspace_id or "default", worker_id="inline")
        else:
            job_id = uuid4().hex
        record: Dict[str, Any] = {
            "job_id": job_id,
            "captain": captain,
            "command": command,
            "session_id": session_id,
            "workspace_id": workspace_id,
            "status": "running",
        }
        self._jobs[job_id] = record
        beat = heartbeat(store, job_id, workspace_id=workspace_id or "default") if store is not None else nullcontext()
        with job_event_context(job_id, workspace_id), beat:
            try:
                result = self._dispatch(captain, command, payload)
            except Exception as exc:
                record.update(status="failed", error=str(exc))
                if store is not None:
                    store.mark_failed(job_id, str(exc), workspace_id=workspace_id or "default")
                logger.warning("job %s (%s.%s) failed: %s", job_id, captain, command, exc)
                raise
        summary = result if isinstance(result, dict) else {"result": result}
        record.update(status="completed", result_summary=summary)
        if store is not None:
            store.mark_completed(job_id, summary, workspace_id=workspace_id or "default")
        return dict(record)

    def get_job(self, job_id: str, workspace_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        store = self.store
        if store is not None:
            return store.get_job(job_id, workspace_id=workspace_id)
        record = self._jobs.get(job_id)
        if record is None:
            return None
        if workspace_id and record.get("workspace_id") not in (None, workspace_id):
            return None
        return dict(record)

    def list_jobs(self, limit: int = 100, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        store = self.store
        if store is not None:
            return store.list_jobs(limit=limit, workspace_id=workspace_id)
        rows = [dict(r) for r in self._jobs.values() if not workspace_id or r.get("workspace_id") in (None, workspace_id)]
        return rows[-limit:][::-1]


_default = InlineQueue()
_factory: Optional[Callable[[], JobQueue]] = None


def set_job_queue(factory: Optional[Callable[[], JobQueue]]) -> None:
    """Install the queue the captains enqueue on; ``None`` restores the inline default."""
    global _factory
    _factory = factory


def job_queue() -> JobQueue:
    return _factory() if _factory is not None else _default


class StoreQueue:
    """Enqueue = write the job to the job store as ``queued``; a worker claims and runs it.

    What the open API uses, so a request that starts a scan or a plant returns at
    once and ``compass serve`` does the work (``compass_core.serve``)."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def enqueue(
        self,
        *,
        captain: str,
        command: str,
        payload: Dict[str, Any],
        session_id: Optional[str] = None,
        queue: Optional[str] = None,
        enforce_single_active_session: bool = False,
    ) -> Dict[str, Any]:
        check_enqueue(captain, payload)
        workspace_id = str(payload.get("workspace_id") or "default") if isinstance(payload, dict) else "default"
        if enforce_single_active_session and session_id:
            active = self.store.find_active_job(session_id=session_id, workspace_id=workspace_id, captain=captain, command=command)
            if active:
                return active
        return self.store.create_job(
            captain=captain, command=command, payload=dict(payload), queue=queue or "compass",
            workspace_id=workspace_id, org_id=payload.get("org_id"), user_id=payload.get("user_id"), session_id=session_id,
        )

    def get_job(self, job_id: str, workspace_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return self.store.get_job(job_id, workspace_id=workspace_id)

    def list_jobs(self, limit: int = 100, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.store.list_jobs(limit=limit, workspace_id=workspace_id)


class QueueProxy:
    """A module-level stand-in for "the job queue", resolved on each use.

    Routers keep one ``queue_client`` attribute as they always have; it follows
    whatever ``set_job_queue`` installed (the product's Celery client, or the
    inline queue), and a test can still patch a method on it. The common
    methods are real methods that delegate at call time: a patch restored by
    monkeypatch then puts back a method of the proxy, not of whichever queue
    happened to be installed when the patch was made."""

    def enqueue(self, **kwargs: Any) -> Dict[str, Any]:
        return job_queue().enqueue(**kwargs)

    def enqueue_and_wait(self, **kwargs: Any) -> Any:
        return job_queue().enqueue_and_wait(**kwargs)  # type: ignore[attr-defined]

    def get_job(self, job_id: str, workspace_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return job_queue().get_job(job_id, workspace_id=workspace_id)

    def list_jobs(self, limit: int = 100, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return job_queue().list_jobs(limit=limit, workspace_id=workspace_id)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> Any:
        return getattr(job_queue(), name)


queue_proxy = QueueProxy()
