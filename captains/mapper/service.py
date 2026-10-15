"""The map re-scan loop ``compass serve`` runs when an interval is set."""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger("compass.mapper.service")


def run_service(workspace_id: str, config: Dict[str, Any], stop: threading.Event, *, interval: Optional[int] = None) -> None:
    """Re-scan every ``interval`` seconds; tick first, then wait, so a server in
    ``once`` mode scans exactly once."""
    from compass_core import dispatch

    seconds = int(interval or 0)
    if seconds <= 0:
        return
    while True:
        try:
            dispatch.dispatch("mapper", "run", {"workspace_id": workspace_id})
            logger.info("map re-scan completed")
        except Exception as exc:  # noqa: BLE001
            logger.warning("map re-scan failed: %s", exc)
        if stop.wait(seconds):
            return
