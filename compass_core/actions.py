"""Actions: the named writes a captain may propose, and the registry of them
(docs/specs/access-model.md §3.1, §8).

An action is one kind of change in the target account, with the IAM statements
it needs for a given scope, a check that a target is inside that scope, and
the four steps the executor runs (snapshot, execute, verify, rollback). A
captain's manifest names the action types it may propose
(``permissions.aws.actions``); its actions module registers the classes with
``register(captain, {type: cls})``, the way its commands module registers
commands. A whitelist row (a grant, once the grant layer lands) names an
action by type; nothing outside the registry can run, and nothing outside the
enabled rows is even permitted in the act role.

The decoy captain's ``ResponseAction`` is the first kind; its types keep their
bare names (``iam_deactivate_key``). A type registered by any other captain is
``<captain>.<name>``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple, Type


class Action:
    """Base of every action. Subclasses set the class attributes and implement the
    whitelist pair and the four steps; ``scope`` and ``ctx`` are whatever the captain
    defines (the decoy captain's ``ActionScope`` and ``ActionContext``)."""

    action_type: str = ""
    title: str = ""
    description: str = ""
    change_type: str = ""           # what kind of change: credential, compute, network, ...
    rollback_supported: bool = False
    rollback_automatic: bool = False
    verify_timeout_seconds: int = 300

    def describe(self) -> Dict[str, Any]:
        return {
            "action_type": self.action_type,
            "title": self.title,
            "description": self.description,
            "change_type": self.change_type,
            "rollback_supported": self.rollback_supported,
            "rollback_automatic": self.rollback_automatic,
            "scope_fields": self.scope_fields(),
        }

    def scope_fields(self) -> List[str]:
        return []

    # --- the whitelist: what a grant of this action allows ------------------
    def iam_statements(self, scope: Any, *, account_id: str, region: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def check_scope(self, scope: Any, ctx: Any) -> None:
        raise NotImplementedError

    # --- the four steps ------------------------------------------------------
    def snapshot(self, session: Any, ctx: Any) -> Dict[str, Any]:
        raise NotImplementedError

    def execute(self, session: Any, ctx: Any, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def verify(self, session: Any, ctx: Any, snapshot: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def rollback(self, session: Any, ctx: Any, snapshot: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


_registry: Dict[str, Tuple[str, Type[Action]]] = {}


def register(captain: str, actions: Mapping[str, Type[Action]]) -> None:
    """Register a captain's action classes by type. Re-registering the same class is a
    no-op; another captain claiming a type is an error, so two captains cannot both
    offer ``iam_deactivate_key``."""
    for action_type, cls in actions.items():
        owner, existing = _registry.get(action_type, (None, None))
        if existing is not None and (owner != captain or existing is not cls) and existing.__qualname__ != cls.__qualname__:
            raise ValueError(f"action type {action_type!r} is already registered by {owner}")
        _registry[action_type] = (captain, cls)


def get_action(action_type: str) -> Action:
    try:
        return _registry[action_type][1]()
    except KeyError:
        raise KeyError(f"Unknown action type: {action_type}") from None


def captain_of(action_type: str) -> Optional[str]:
    found = _registry.get(action_type)
    return found[0] if found else None


def registered(captain: Optional[str] = None) -> List[str]:
    return sorted(t for t, (owner, _) in _registry.items() if captain is None or owner == captain)


def catalog(captain: Optional[str] = None) -> List[Dict[str, Any]]:
    """What the Captains page shows under "Will use" and the whitelist offers."""
    return [{**cls().describe(), "captain": owner} for t, (owner, cls) in sorted(_registry.items()) if captain is None or owner == captain]


def describe_action(action_type: str) -> Dict[str, Any]:
    found = _registry.get(action_type)
    if not found:
        return {"action_type": action_type, "registered": False}
    return {**found[1]().describe(), "captain": found[0], "registered": True}
