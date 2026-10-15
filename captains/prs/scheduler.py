"""The ticker: every ``PRS_TICK_SECONDS`` enqueue one ``prs.sync`` per workspace.

Runs as a background thread in the API process (docs/pr_watcher_spec.md §5).
Duplicate ticks from several API replicas collapse on the job's session id.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("prs.scheduler")

_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def tick_seconds() -> int:
    try:
        return max(0, int(os.getenv("PRS_TICK_SECONDS", "300") or 0))
    except ValueError:
        return 300


def workspace_ids() -> List[str]:
    table_name = (os.getenv("WORKSPACES_TABLE") or "").strip()
    if not table_name:
        default = os.getenv("DEFAULT_WORKSPACE") or os.getenv("TARGET")
        return [default] if default else []
    import boto3
    from boto3.dynamodb.conditions import Attr

    table = boto3.resource("dynamodb", region_name=os.getenv("WORKSPACES_TABLE_REGION") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")).Table(table_name)
    hash_key = os.getenv("WORKSPACES_TABLE_HASH_KEY", "pk")
    range_key = os.getenv("WORKSPACES_TABLE_RANGE_KEY", "sk")
    ids: List[str] = []
    kwargs = {"FilterExpression": Attr(range_key).begins_with("WORKSPACE#") & Attr(hash_key).begins_with("WORKSPACE#"), "ProjectionExpression": "workspace_id, #sk", "ExpressionAttributeNames": {"#sk": range_key}}
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            ws = item.get("workspace_id") or str(item.get(range_key) or "").split("#", 1)[-1]
            if ws and ws not in ids:
                ids.append(str(ws))
        last = resp.get("LastEvaluatedKey")
        if not last or len(ids) >= 500:
            break
        kwargs["ExclusiveStartKey"] = last
    return ids


def tick(workspaces: Optional[Iterable[str]] = None, *, client=None) -> int:
    """Enqueue ``prs.sync`` for each workspace; returns how many were enqueued (dedupe counts as 0 new)."""
    if client is None:
        from compass_core.queue import job_queue

        client = job_queue()
    enqueued = 0
    for workspace_id in (workspaces if workspaces is not None else workspace_ids()):
        try:
            job = client.enqueue(captain="prs", command="sync", payload={"workspace_id": workspace_id, "trigger": "tick"}, session_id="prs-sync", enforce_single_active_session=True)
            if str(job.get("status") or "") == "queued":
                enqueued += 1
        except Exception as exc:
            logger.warning("[prs.scheduler] enqueue for %s failed: %s", workspace_id, exc)
    return enqueued


def _loop(interval: int) -> None:
    while not _stop.wait(interval):
        try:
            tick()
        except Exception as exc:
            logger.warning("[prs.scheduler] tick failed: %s", exc)


def start_ticker() -> Optional[threading.Thread]:
    global _thread
    interval = tick_seconds()
    if interval <= 0:
        logger.info("[prs.scheduler] disabled (PRS_TICK_SECONDS=0)")
        return None
    if _thread and _thread.is_alive():
        return _thread
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(interval,), name="prs-ticker", daemon=True)
    _thread.start()
    logger.info("[prs.scheduler] ticking every %ss", interval)
    return _thread


def stop_ticker() -> None:
    _stop.set()


def run_service(workspace_id: str, config: Dict[str, Any], stop: threading.Event, *, interval: Optional[int] = None) -> None:
    """The tick as a `compass serve` service: tick first, then wait ``interval``
    seconds, until ``stop`` is set (compass_core.captains.ServiceSpec)."""
    seconds = int(interval if interval is not None else tick_seconds())
    if seconds <= 0:
        return
    while True:
        try:
            ran = tick([workspace_id])
            logger.info("prs tick: %d sync(s) ran", ran)
        except Exception as exc:  # noqa: BLE001
            logger.warning("prs tick failed: %s", exc)
        if stop.wait(seconds):
            return
