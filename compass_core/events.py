"""Progress events for the job that is running, without knowing where jobs run.

A captain calls ``emit_job_event`` as it works. Which job it is comes from a
context variable that the runner sets with ``job_event_context``; where the
event goes is the installed ``EventSink``. The open default logs one line per
event; ``compass serve`` replaces it with ``StoreSink``, which also appends the
event to the job in the jobs table, so the dashboard sees a job's progress. The
product's worker installs a sink of its own (a larger deployment installs its
own with ``set_event_sink``). Outside a job context, events are dropped.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Dict, Iterator, Optional, Protocol

logger = logging.getLogger("compass.events")


class EventSink(Protocol):
    def append_event(
        self,
        job_id: str,
        message: str,
        *,
        event_type: str,
        status: Optional[str],
        data: Optional[Dict[str, Any]],
        workspace_id: Optional[str],
    ) -> None: ...


class LogSink:
    """The open default: one log line per event."""

    def append_event(self, job_id, message, *, event_type, status, data, workspace_id) -> None:
        suffix = f" [{status}]" if status else ""
        logger.info("job %s: %s%s", job_id, message, suffix)


class StoreSink(LogSink):
    """The log line, and the event on the job in the job store (``CaptainJobStore.append_event``)."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def append_event(self, job_id, message, *, event_type, status, data, workspace_id) -> None:
        super().append_event(job_id, message, event_type=event_type, status=status, data=data, workspace_id=workspace_id)
        self.store.append_event(job_id, message, event_type=event_type, status=status, data=data, workspace_id=workspace_id)


_sink: EventSink = LogSink()


def set_event_sink(sink: EventSink) -> None:
    global _sink
    _sink = sink


def event_sink() -> EventSink:
    return _sink


_current_job_id: ContextVar[Optional[str]] = ContextVar("current_job_id", default=None)
_current_workspace_id: ContextVar[Optional[str]] = ContextVar("current_workspace_id", default=None)


def _set_current_job(job_id: Optional[str]) -> Optional[Token]:
    if job_id is None:
        return None
    return _current_job_id.set(job_id)


def _set_current_workspace(workspace_id: Optional[str]) -> Optional[Token]:
    if workspace_id is None:
        return None
    return _current_workspace_id.set(workspace_id)


def _reset_current(token: Optional[Token], *, var_name: str) -> None:
    if not token:
        return
    try:
        if var_name == "job":
            _current_job_id.reset(token)
        else:
            _current_workspace_id.reset(token)
    except LookupError:
        logger.debug("Attempted to reset missing %s context token", var_name)


def current_job_id() -> Optional[str]:
    return _current_job_id.get()


def current_workspace_id() -> Optional[str]:
    return _current_workspace_id.get()


def emit_job_event(
    message: str,
    *,
    event_type: str = "info",
    status: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    job_id = _current_job_id.get()
    workspace_id = _current_workspace_id.get()
    if not job_id:
        return
    try:
        _sink.append_event(
            job_id,
            message,
            event_type=event_type,
            status=status,
            data=data,
            workspace_id=workspace_id,
        )
    except Exception as exc:
        logger.debug("Failed to append job event for %s: %s", job_id, exc)


@contextmanager
def job_event_context(job_id: Optional[str], workspace_id: Optional[str] = None) -> Iterator[None]:
    job_token = _set_current_job(job_id)
    workspace_token = _set_current_workspace(workspace_id)
    try:
        yield
    finally:
        _reset_current(job_token, var_name="job")
        _reset_current(workspace_token, var_name="workspace")
