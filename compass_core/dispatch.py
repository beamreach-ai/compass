"""The open command registry: ``(captain, command) -> handler(payload)``.

Each open package registers its commands on import (``captains/<name>/commands.py``).
``dispatch`` runs one; ``use_inline_queue`` makes follow-up jobs run through
the same table in-process, so a chain like alert → investigate → respond works
with no broker. The product's dispatcher keeps its own table and queue.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Tuple  # noqa: F401 - Any used in signatures

from compass_core.queue import InlineQueue, set_job_queue

Handler = Callable[[Dict[str, Any]], Dict[str, Any]]

_registry: Dict[Tuple[str, str], Handler] = {}


def register(captain: str, commands: Mapping[str, Handler]) -> None:
    for command, handler in commands.items():
        _registry[(captain, command)] = handler


def commands() -> List[Tuple[str, str]]:
    return sorted(_registry)


def dispatch(captain: str, command: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        handler = _registry[(captain, command)]
    except KeyError:
        known = ", ".join(f"{c}.{m}" for c, m in commands()) or "none registered"
        raise KeyError(f"unknown command {captain}.{command}; known: {known}") from None
    # a job that reaches a worker for a captain turned off after it was queued fails here, with the reason
    from compass_core import captains as _captains

    _captains.ensure_active_for_payload(captain, payload)
    return handler(dict(payload))


def _default_store():
    """The job store of the default workspace (dynamodb_config.jobs), resolved lazily."""
    from compass_core.config import workspace_config
    from compass_core.jobs import store_for
    from compass_core.workspace import default_workspace_id

    workspace_id = default_workspace_id()
    return store_for(workspace_config(workspace_id)) if workspace_id else None


def use_inline_queue(*, store: Any = None) -> InlineQueue:
    """Route enqueued jobs through this registry, synchronously, recording each job in
    the workspace's job store when it has one. Returns the queue."""
    queue = InlineQueue(dispatch, store=store, store_factory=None if store is not None else _default_store)
    set_job_queue(lambda: queue)
    return queue
