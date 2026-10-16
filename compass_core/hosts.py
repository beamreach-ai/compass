"""Hosts: distributions that build a product on top of Compass.

A captain adds one capability; a host changes how the whole install behaves:
where workspaces come from, which queue runs jobs, which model answers, extra
API routers. A host registers a function under the ``compass.hosts`` entry
point group (or ``COMPASS_HOSTS``, comma-separated ``module:function`` refs,
for a checkout that is not installed). ``install()`` calls each once, before
the CLI dispatches and before the API is assembled, so ``compass api`` and
``compass serve`` become the host's API and daemon when the host is installed.

A host function takes no arguments and does its work through the existing
seams: ``compass_core.queue.set_job_queue``, ``compass_core.config``,
``compass_core.llm``, ``compass_api.extensions``, ``compass_core.captains.register``.
"""

from __future__ import annotations

import logging
import os
import threading
from importlib import metadata
from typing import Callable, Dict, List, Tuple

logger = logging.getLogger("compass.hosts")

ENTRY_POINT_GROUP = "compass.hosts"

_lock = threading.Lock()
_installed: Dict[str, str] = {}   # name -> "ok" or the error
_done = False


def _candidates() -> List[Tuple[str, Callable[[], Callable[[], None]]]]:
    from compass_core.captains import load_reference

    found: List[Tuple[str, Callable[[], Callable[[], None]]]] = []
    try:
        for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
            found.append((ep.name, ep.load))
    except Exception as exc:  # noqa: BLE001
        logger.warning("host entry points could not be read: %s", exc)
    for ref in (os.getenv("COMPASS_HOSTS") or "").split(","):
        ref = ref.strip()
        if ref:
            found.append((ref, lambda ref=ref: load_reference(ref)))
    return found


def install() -> Dict[str, str]:
    """Run every host's function once per process; returns name -> outcome.

    A host that fails is logged and recorded, and the open defaults stay in
    effect for whatever it did not set: a broken host never keeps Compass from starting."""
    global _done
    with _lock:
        if _done:
            return dict(_installed)
        _done = True
        for name, loader in _candidates():
            if name in _installed:
                continue
            try:
                loader()()
                _installed[name] = "ok"
                logger.info("host %s installed", name)
            except Exception as exc:  # noqa: BLE001
                _installed[name] = f"{type(exc).__name__}: {exc}"
                logger.error("host %s failed to install: %s", name, exc)
        return dict(_installed)


def installed() -> List[str]:
    """The hosts that installed successfully."""
    return [name for name, outcome in _installed.items() if outcome == "ok"]


def reset_for_tests() -> None:
    global _done
    with _lock:
        _installed.clear()
        _done = False
