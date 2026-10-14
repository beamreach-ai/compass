"""Deactivate the honeytoken access key that was used.

The key is deny-all already, so this changes nothing the intruder could do;
it ends the stream of CloudTrail events and marks the token as spent. The key
stays inactive until the token is re-armed, so rollback is never automatic.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from captains.honeypot.actions.base import ActionContext, ActionScope, ResponseAction, ScopeError, parse_arn


class IamDeactivateKey(ResponseAction):
    action_type = "iam_deactivate_key"
    title = "Deactivate the honeytoken access key"
    description = "Set the used access key to Inactive. It stays inactive until the honeytoken is re-armed."
    change_type = "IAM_KEY_DEACTIVATE"
    rollback_supported = True
    rollback_automatic = False
    verify_timeout_seconds = 60

    def scope_fields(self) -> List[str]:
        return ["user_arns", "user_path_prefixes"]

    # --- whitelist -----------------------------------------------------------
    def iam_statements(self, scope: ActionScope, *, account_id: str, region: str) -> List[Dict[str, Any]]:
        resources = list(scope.user_arns)
        for prefix in scope.user_path_prefixes:
            path = prefix if prefix.startswith("/") else f"/{prefix}"
            if not path.endswith("/"):
                path += "/"
            resources.append(f"arn:aws:iam::{account_id}:user{path}*")
        if not resources:
            return []
        return [
            {
                "Sid": "DeactivateScopedKeys",
                "Effect": "Allow",
                "Action": ["iam:UpdateAccessKey", "iam:ListAccessKeys"],
                "Resource": resources,
            }
        ]

    def check_scope(self, scope: ActionScope, ctx: ActionContext) -> None:
        if ctx.user_arn and ctx.user_arn in scope.user_arns:
            return
        p = parse_arn(ctx.user_arn)
        path = p.get("resource", "") if p else ""
        for prefix in scope.user_path_prefixes:
            norm = prefix.strip("/")
            if path.startswith(f"user/{norm}/"):
                return
        raise ScopeError(f"user {ctx.user_arn or '<none>'} is not in this action's scope")

    # --- steps ---------------------------------------------------------------
    def snapshot(self, session: Any, ctx: ActionContext) -> Dict[str, Any]:
        iam = session.client("iam", region_name=ctx.region)
        keys = iam.list_access_keys(UserName=ctx.user_name)["AccessKeyMetadata"]
        return {
            "taken_at": datetime.now(timezone.utc).isoformat(),
            "user_name": ctx.user_name,
            "keys": [{"access_key_id": k["AccessKeyId"], "status": k["Status"]} for k in keys],
        }

    def execute(self, session: Any, ctx: ActionContext, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        iam = session.client("iam", region_name=ctx.region)
        resp = iam.update_access_key(UserName=ctx.user_name, AccessKeyId=ctx.access_key_id, Status="Inactive")
        return {
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "request_id": (resp.get("ResponseMetadata") or {}).get("RequestId"),
            "access_key_id": ctx.access_key_id,
        }

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
        iam = session.client("iam", region_name=ctx.region)
        deadline = clock() + (timeout_seconds or self.verify_timeout_seconds)
        while clock() < deadline:
            keys = iam.list_access_keys(UserName=ctx.user_name)["AccessKeyMetadata"]
            for k in keys:
                if k["AccessKeyId"] == ctx.access_key_id and k["Status"] == "Inactive":
                    return {"ok": True, "verified_at": datetime.now(timezone.utc).isoformat(), "status": "Inactive"}
            sleep(1)
        return {"ok": False, "verified_at": None, "reason": "key still active after timeout"}

    def rollback(self, session: Any, ctx: ActionContext, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        iam = session.client("iam", region_name=ctx.region)
        iam.update_access_key(UserName=ctx.user_name, AccessKeyId=ctx.access_key_id, Status="Active")
        return {"supported": True, "done_at": datetime.now(timezone.utc).isoformat(), "status": "Active"}

    # --- scoring -------------------------------------------------------------
    def risk_inputs(self, ctx: ActionContext, snapshot: Dict[str, Any]) -> Tuple[Sequence[Dict[str, Any]], Dict[str, Any]]:
        resources = [{"type": "iam_access_key", "id": ctx.access_key_id, "env": ctx.environment}]
        evidence = {"summary": "deactivate a deny-all honeytoken key", "evidence_snapshotted": True, "metrics": [], "assumptions": []}
        return resources, evidence
