"""Response actions: the whitelist of what Compass may do in a target account.

Each action type is a catalog entry declaring the IAM it needs, how to take a
before-snapshot, how to execute, how to verify, and how to roll back. The
whitelist rows a workspace enables (``captains.engine.api.routes.honeypot_actions``)
reference these by ``action_type``; the responder IAM role in the target account
is generated from the enabled rows, so nothing outside the catalog can run and
nothing outside the enabled rows is even permitted.
"""

from __future__ import annotations

from typing import Dict, List, Type

from captains.honeypot.actions.base import ActionContext, ActionScope, ResponseAction, ScopeError
from captains.honeypot.actions.ecs_restart_task import EcsRestartTask
from captains.honeypot.actions.iam_deactivate_key import IamDeactivateKey

ACTION_CATALOG: Dict[str, Type[ResponseAction]] = {
    EcsRestartTask.action_type: EcsRestartTask,
    IamDeactivateKey.action_type: IamDeactivateKey,
}


def get_action(action_type: str) -> ResponseAction:
    try:
        return ACTION_CATALOG[action_type]()
    except KeyError:
        raise KeyError(f"Unknown response action type: {action_type}") from None


def catalog() -> List[Dict[str, object]]:
    return [cls().describe() for cls in ACTION_CATALOG.values()]


__all__ = [
    "ACTION_CATALOG",
    "ActionContext",
    "ActionScope",
    "ResponseAction",
    "ScopeError",
    "catalog",
    "get_action",
]
