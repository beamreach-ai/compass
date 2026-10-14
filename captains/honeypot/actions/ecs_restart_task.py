"""Stop the ECS task that used a honeytoken; the service replaces it from the
clean image.

Spike A7: a Fargate replacement reaches RUNNING in ~37 s and the service
reports steady state at ~42 s. The reason passed to StopTask is visible to
anyone with ecs:DescribeTasks, so it is a neutral ``incident-<id>``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from captains.honeypot.actions.base import (
    ActionContext,
    ActionScope,
    ResponseAction,
    ScopeError,
    cluster_arn_of_service,
    cluster_name_of,
    parse_arn,
)


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value) if value else None


def _task_id(task_arn: str) -> str:
    return (task_arn or "").rsplit("/", 1)[-1]


class EcsRestartTask(ResponseAction):
    action_type = "ecs_restart_task"
    title = "Restart the ECS task"
    description = (
        "Stop the task that used the honeytoken. The service starts a replacement "
        "from the clean image; the intruder's shell dies with the task."
    )
    change_type = "ECS_TASK_RESTART"
    rollback_supported = False
    verify_timeout_seconds = 300

    def scope_fields(self) -> List[str]:
        return ["service_arns"]

    # --- whitelist -----------------------------------------------------------
    def iam_statements(self, scope: ActionScope, *, account_id: str, region: str) -> List[Dict[str, Any]]:
        clusters = scope.cluster_arns
        if not scope.service_arns or not clusters:
            return []
        task_arns = []
        for cluster in clusters:
            p = parse_arn(cluster)
            task_arns.append(f"arn:{p['partition']}:ecs:{p['region']}:{p['account']}:task/{cluster_name_of(cluster)}/*")
        return [
            {
                "Sid": "StopTasksOfScopedServices",
                "Effect": "Allow",
                "Action": ["ecs:StopTask"],
                "Resource": task_arns,
                "Condition": {"ArnEquals": {"ecs:cluster": clusters}},
            },
            {
                "Sid": "ReadScopedServices",
                "Effect": "Allow",
                "Action": ["ecs:DescribeServices"],
                "Resource": list(scope.service_arns),
            },
            {
                "Sid": "ReadTasksOfScopedClusters",
                "Effect": "Allow",
                "Action": ["ecs:DescribeTasks", "ecs:ListTasks"],
                "Resource": "*",
                "Condition": {"ArnEquals": {"ecs:cluster": clusters}},
            },
            {
                "Sid": "ReadTaskDefinitionsAndNetwork",
                "Effect": "Allow",
                "Action": ["ecs:DescribeTaskDefinition", "ec2:DescribeNetworkInterfaces"],
                "Resource": "*",
            },
        ]

    def check_scope(self, scope: ActionScope, ctx: ActionContext) -> None:
        if ctx.service_arn not in scope.service_arns:
            raise ScopeError(f"service {ctx.service_arn or '<none>'} is not in this action's scope")
        cluster = ctx.cluster_arn or cluster_arn_of_service(ctx.service_arn)
        if not ctx.task_arn or not ctx.task_arn.startswith("arn:") or cluster_name_of(cluster) not in ctx.task_arn:
            raise ScopeError(f"task {ctx.task_arn or '<none>'} does not belong to the scoped service's cluster")

    # --- steps ---------------------------------------------------------------
    def snapshot(self, session: Any, ctx: ActionContext) -> Dict[str, Any]:
        ecs = session.client("ecs", region_name=ctx.region)
        cluster = ctx.cluster_arn or cluster_arn_of_service(ctx.service_arn)
        service = ecs.describe_services(cluster=cluster, services=[ctx.service_arn])["services"][0]
        task = ecs.describe_tasks(cluster=cluster, tasks=[ctx.task_arn])["tasks"][0]
        task_def = ecs.describe_task_definition(taskDefinition=task["taskDefinitionArn"])["taskDefinition"]
        running = ecs.list_tasks(cluster=cluster, serviceName=service["serviceName"], desiredStatus="RUNNING").get("taskArns", [])
        log_streams = _log_pointers(task_def, ctx.task_arn)
        return {
            "taken_at": datetime.now(timezone.utc).isoformat(),
            "cluster_arn": cluster,
            "service": {
                "name": service["serviceName"],
                "desired_count": service.get("desiredCount"),
                "running_count": service.get("runningCount"),
                "task_definition": service.get("taskDefinition"),
                "network_configuration": service.get("networkConfiguration"),
                "deployments": [
                    {"status": d.get("status"), "task_definition": d.get("taskDefinition"), "running": d.get("runningCount")}
                    for d in service.get("deployments", [])
                ],
            },
            "task": {
                "arn": ctx.task_arn,
                "status": task.get("lastStatus"),
                "started_at": _iso(task.get("startedAt")),
                "task_definition": task.get("taskDefinitionArn"),
                "containers": [
                    {"name": c.get("name"), "image": c.get("image"), "image_digest": c.get("imageDigest")}
                    for c in task.get("containers", [])
                ],
                "attachments": task.get("attachments", []),
            },
            "running_tasks": running,
            "log_streams": log_streams,
        }

    def execute(self, session: Any, ctx: ActionContext, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        ecs = session.client("ecs", region_name=ctx.region)
        reason = f"incident-{ctx.short_incident}"
        resp = ecs.stop_task(cluster=snapshot["cluster_arn"], task=ctx.task_arn, reason=reason)
        return {
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "request_id": (resp.get("ResponseMetadata") or {}).get("RequestId"),
            "stopped_task": ctx.task_arn,
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
        """Done when a task with a new id is RUNNING and the service has reported
        steady state since the stop."""
        ecs = session.client("ecs", region_name=ctx.region)
        cluster = snapshot["cluster_arn"]
        service_name = snapshot["service"]["name"]
        started = clock()
        stop_time = _parse_iso(executed.get("executed_at")) or started
        deadline = started + (timeout_seconds or self.verify_timeout_seconds)
        new_task: Optional[str] = None
        new_running_at: Optional[float] = None
        while clock() < deadline:
            arns = ecs.list_tasks(cluster=cluster, serviceName=service_name, desiredStatus="RUNNING").get("taskArns", [])
            candidates = [a for a in arns if a != ctx.task_arn]
            if candidates and new_task is None:
                tasks = ecs.describe_tasks(cluster=cluster, tasks=candidates)["tasks"]
                for t in tasks:
                    if t.get("lastStatus") == "RUNNING":
                        new_task = t["taskArn"]
                        new_running_at = clock()
                        break
            if new_task is not None:
                svc = ecs.describe_services(cluster=cluster, services=[ctx.service_arn])["services"][0]
                for event in svc.get("events", [])[:5]:
                    created = event.get("createdAt")
                    created_ts = created.timestamp() if isinstance(created, datetime) else None
                    if "steady state" in (event.get("message") or "") and created_ts and created_ts >= stop_time - 1:
                        return {
                            "ok": True,
                            "verified_at": datetime.now(timezone.utc).isoformat(),
                            "new_task": new_task,
                            "new_task_running_after_s": round((new_running_at or clock()) - started, 1),
                            "steady_after_s": round(clock() - started, 1),
                            "running_count": svc.get("runningCount"),
                            "desired_count": svc.get("desiredCount"),
                        }
            sleep(2)
        return {
            "ok": False,
            "verified_at": None,
            "new_task": new_task,
            "reason": "timeout waiting for a replacement task and steady state",
            "waited_s": round(clock() - started, 1),
        }

    def rollback(self, session: Any, ctx: ActionContext, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        return {"supported": False, "reason": "ECS already replaced the task; there is nothing to restore"}

    # --- scoring -------------------------------------------------------------
    def risk_inputs(self, ctx: ActionContext, snapshot: Dict[str, Any]) -> Tuple[Sequence[Dict[str, Any]], Dict[str, Any]]:
        resources = [{"type": "ecs_task", "id": _task_id(ctx.task_arn), "env": ctx.environment}]
        evidence = {
            "summary": "stop one task; the service replaces it",
            "desired_count": (snapshot.get("service") or {}).get("desired_count"),
            "running_count": (snapshot.get("service") or {}).get("running_count"),
            "evidence_snapshotted": True,
            "metrics": [],
            "assumptions": [],
        }
        return resources, evidence


def _log_pointers(task_def: Dict[str, Any], task_arn: str) -> List[Dict[str, str]]:
    """Where the task's logs live, so the incident can link to them. Exporting
    them to an evidence bucket is HP-17's job once the bucket exists."""
    out = []
    for c in task_def.get("containerDefinitions", []):
        cfg = (c.get("logConfiguration") or {})
        opts = cfg.get("options") or {}
        if cfg.get("logDriver") == "awslogs" and opts.get("awslogs-group"):
            prefix = opts.get("awslogs-stream-prefix")
            stream = f"{prefix}/{c.get('name')}/{_task_id(task_arn)}" if prefix else None
            out.append({"container": c.get("name"), "log_group": opts["awslogs-group"], "log_stream": stream or ""})
    return out


def _parse_iso(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
