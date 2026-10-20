from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from compass_core.actions import Action


class ScopeError(ValueError):
    """The target of an action is outside the whitelist row's scope."""


@dataclass
class ActionScope:
    """What a whitelist row allows an action to touch.

    ``service_arns`` scope ECS actions to specific services (and, through their
    cluster, the tasks of those services). ``user_arns`` scope IAM actions to
    specific users; ``user_path_prefixes`` allow every user under a path, which
    is how honeytoken users (``/service/``) are covered without listing each.
    """

    service_arns: List[str] = field(default_factory=list)
    user_arns: List[str] = field(default_factory=list)
    user_path_prefixes: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "ActionScope":
        raw = raw or {}
        return cls(
            service_arns=[str(a) for a in raw.get("service_arns") or [] if a],
            user_arns=[str(a) for a in raw.get("user_arns") or [] if a],
            user_path_prefixes=[str(p) for p in raw.get("user_path_prefixes") or [] if p],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service_arns": list(self.service_arns),
            "user_arns": list(self.user_arns),
            "user_path_prefixes": list(self.user_path_prefixes),
        }

    @property
    def cluster_arns(self) -> List[str]:
        out: List[str] = []
        for arn in self.service_arns:
            cluster = cluster_arn_of_service(arn)
            if cluster and cluster not in out:
                out.append(cluster)
        return out


@dataclass
class ActionContext:
    """Everything an action needs to know about where it acts.

    Filled by the incident's correlation (HP-08): the task that tripped, its
    service and cluster, and the honeytoken principal that was used.
    """

    region: str
    incident_id: str
    account_id: str = ""
    cluster_arn: str = ""
    service_arn: str = ""
    task_arn: str = ""
    container: str = ""
    user_name: str = ""
    user_arn: str = ""
    access_key_id: str = ""
    environment: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def short_incident(self) -> str:
        """Incident ids are <epoch ms>-<hex>; the hex part is enough to find
        the incident again and reveals nothing about when it happened."""
        return (self.incident_id or "").rsplit("-", 1)[-1][:12] or "unknown"


def parse_arn(arn: str) -> Dict[str, str]:
    parts = (arn or "").split(":", 5)
    if len(parts) < 6:
        return {}
    return {"partition": parts[1], "service": parts[2], "region": parts[3], "account": parts[4], "resource": parts[5]}


def cluster_arn_of_service(service_arn: str) -> str:
    """``arn:aws:ecs:r:a:service/<cluster>/<name>`` → ``arn:aws:ecs:r:a:cluster/<cluster>``."""
    p = parse_arn(service_arn)
    if not p or p["service"] != "ecs" or not p["resource"].startswith("service/"):
        return ""
    bits = p["resource"].split("/")
    if len(bits) != 3:
        return ""
    return f"arn:{p['partition']}:ecs:{p['region']}:{p['account']}:cluster/{bits[1]}"


def cluster_name_of(cluster_arn: str) -> str:
    p = parse_arn(cluster_arn)
    return p["resource"].split("/", 1)[1] if p and "/" in p["resource"] else ""


class ResponseAction(Action):
    """A decoy response: a ``compass_core.actions.Action`` whose scope is an
    ``ActionScope`` and whose context an ``ActionContext``. Subclasses implement
    the whitelist pair and the four steps.

    The executor always runs ``snapshot`` → ``execute`` → ``verify`` and writes
    an audit record; ``rollback`` is offered to the user afterwards when
    ``rollback_supported`` is true.
    """

    # --- whitelist -----------------------------------------------------------
    def iam_statements(self, scope: ActionScope, *, account_id: str, region: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def check_scope(self, scope: ActionScope, ctx: ActionContext) -> None:
        raise NotImplementedError

    # --- the four steps ------------------------------------------------------
    def snapshot(self, session: Any, ctx: ActionContext) -> Dict[str, Any]:
        raise NotImplementedError

    def execute(self, session: Any, ctx: ActionContext, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def verify(
        self,
        session: Any,
        ctx: ActionContext,
        snapshot: Dict[str, Any],
        executed: Dict[str, Any],
        *,
        timeout_seconds: Optional[int] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def rollback(self, session: Any, ctx: ActionContext, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        return {"supported": False, "reason": "no rollback for this action"}

    # --- scoring inputs ------------------------------------------------------
    def risk_inputs(self, ctx: ActionContext, snapshot: Dict[str, Any]) -> Tuple[Sequence[Dict[str, Any]], Dict[str, Any]]:
        raise NotImplementedError
