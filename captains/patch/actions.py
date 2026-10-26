"""The canary deploy: the one write the patch captain may make in the account, as an action
the grant flow arms (docs/specs/patching-captain.md §3.6, compass_core.actions).

``patch.canary_deploy`` runs the fix's image as the named staging twin of a service for a
few minutes: a task definition revision with the pull request's image, the staging service
updated to it, steady state awaited, the probe paths fetched and compared with what the
service answered before, then the staging service rolled back to its previous revision.
Its IAM statements name the exact staging services and clusters the workspace configured
(``patch.canaries``), so the act role can touch nothing that serves traffic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from compass_core import actions as _core_actions
from compass_core.actions import Action


class ScopeError(ValueError):
    """The canary's target is outside the grant's scope."""


@dataclass
class CanaryScope:
    """What a grant of the canary allows: the staging services (ARNs) and the task roles their
    task definitions run as, which the act role must be allowed to pass."""

    service_arns: List[str] = field(default_factory=list)
    task_role_arns: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, Any]]) -> "CanaryScope":
        raw = raw or {}
        return cls(service_arns=[str(a) for a in raw.get("service_arns") or [] if a], task_role_arns=[str(a) for a in raw.get("task_role_arns") or [] if a])

    def to_dict(self) -> Dict[str, Any]:
        return {"service_arns": list(self.service_arns), "task_role_arns": list(self.task_role_arns)}

    @property
    def cluster_arns(self) -> List[str]:
        out: List[str] = []
        for arn in self.service_arns:
            cluster = cluster_arn_of_service(arn)
            if cluster and cluster not in out:
                out.append(cluster)
        return out


@dataclass
class CanaryContext:
    """One canary: the staging service, the image to run, the probes, the window."""

    region: str
    account_id: str
    service_arn: str                     # the staging twin
    image: str                           # the pull request's image, in ECR
    container: Optional[str] = None      # the container whose image changes; the first when None
    probe_paths: List[str] = field(default_factory=lambda: ["/"])
    probe_urls: List[str] = field(default_factory=list)   # full URLs to probe besides the paths
    minutes: int = 5
    finding_keys: List[str] = field(default_factory=list)
    pr_url: Optional[str] = None
    probe: Optional[Callable[[str], int]] = None          # GET url -> status code; injected in tests

    @property
    def cluster_arn(self) -> str:
        return cluster_arn_of_service(self.service_arn)


def cluster_arn_of_service(service_arn: str) -> str:
    """``arn:aws:ecs:r:a:service/<cluster>/<service>`` → the cluster's ARN."""
    parts = service_arn.split(":", 5)
    if len(parts) < 6 or not parts[5].startswith("service/"):
        return ""
    tail = parts[5].split("/")
    if len(tail) < 3:
        return ""
    return f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[3]}:{parts[4]}:cluster/{tail[1]}"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class CanaryDeploy(Action):
    action_type = "patch.canary_deploy"
    title = "Run the fix on a staging twin"
    description = ("Register a task definition revision with the pull request's image, run it as the named staging service "
                   "for a few minutes, probe it, compare errors and latency, then roll the staging service back.")
    change_type = "ECS_CANARY_DEPLOY"
    rollback_supported = True
    rollback_automatic = True
    verify_timeout_seconds = 900

    def scope_fields(self) -> List[str]:
        return ["service_arns", "task_role_arns"]

    # --- the whitelist -------------------------------------------------------
    def iam_statements(self, scope: Any, *, account_id: str, region: str) -> List[Dict[str, Any]]:
        scope = scope if isinstance(scope, CanaryScope) else CanaryScope.from_dict(scope)
        clusters = scope.cluster_arns
        if not scope.service_arns or not clusters:
            return []
        statements = [
            {"Sid": "UpdateScopedStagingServices", "Effect": "Allow", "Action": ["ecs:UpdateService", "ecs:DescribeServices"], "Resource": list(scope.service_arns)},
            {"Sid": "RegisterRevisions", "Effect": "Allow", "Action": ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"], "Resource": "*"},
            {"Sid": "ReadTasksOfScopedClusters", "Effect": "Allow", "Action": ["ecs:DescribeTasks", "ecs:ListTasks"], "Resource": "*",
             "Condition": {"ArnEquals": {"ecs:cluster": clusters}}},
            {"Sid": "ReadLoadBalancersAndMetrics", "Effect": "Allow",
             "Action": ["elasticloadbalancing:DescribeTargetGroups", "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTargetHealth",
                        "cloudwatch:GetMetricData"], "Resource": "*"},
        ]
        if scope.task_role_arns:
            statements.append({"Sid": "PassTheTaskRoles", "Effect": "Allow", "Action": ["iam:PassRole"], "Resource": list(scope.task_role_arns),
                               "Condition": {"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}}})
        return statements

    def check_scope(self, scope: Any, ctx: Any) -> None:
        scope = scope if isinstance(scope, CanaryScope) else CanaryScope.from_dict(scope)
        if ctx.service_arn not in scope.service_arns:
            raise ScopeError(f"staging service {ctx.service_arn or '<none>'} is not in this grant's scope")

    # --- the four steps ------------------------------------------------------
    def snapshot(self, session: Any, ctx: CanaryContext) -> Dict[str, Any]:
        ecs = session.client("ecs", region_name=ctx.region)
        service = ecs.describe_services(cluster=ctx.cluster_arn, services=[ctx.service_arn])["services"][0]
        task_def = ecs.describe_task_definition(taskDefinition=service["taskDefinition"])["taskDefinition"]
        urls = self._probe_urls(session, ctx, service)
        before = {url: self._status(ctx, url) for url in urls}
        return {"taken_at": _now(), "cluster_arn": ctx.cluster_arn, "service_name": service["serviceName"], "task_definition": service["taskDefinition"],
                "desired_count": service.get("desiredCount"), "running_count": service.get("runningCount"),
                "containers": [{"name": c.get("name"), "image": c.get("image")} for c in task_def.get("containerDefinitions") or []],
                "task_role_arn": task_def.get("taskRoleArn"), "execution_role_arn": task_def.get("executionRoleArn"),
                "target_groups": [lb.get("targetGroupArn") for lb in service.get("loadBalancers") or [] if lb.get("targetGroupArn")],
                "probe_urls": urls, "before": before}

    def execute(self, session: Any, ctx: CanaryContext, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        ecs = session.client("ecs", region_name=ctx.region)
        task_def = ecs.describe_task_definition(taskDefinition=snapshot["task_definition"])["taskDefinition"]
        containers = list(task_def.get("containerDefinitions") or [])
        target = next((c for c in containers if ctx.container and c.get("name") == ctx.container), None) or (containers[0] if containers else None)
        if target is None:
            raise RuntimeError("the staging task definition has no container to run the image in")
        replaced = target.get("image")
        target["image"] = ctx.image
        keep = ("family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions", "volumes", "placementConstraints", "requiresCompatibilities",
                "cpu", "memory", "pidMode", "ipcMode", "proxyConfiguration", "runtimePlatform", "ephemeralStorage", "inferenceAccelerators")
        request = {k: task_def[k] for k in keep if task_def.get(k) not in (None, [], {})}
        request["containerDefinitions"] = containers
        registered = ecs.register_task_definition(**request)["taskDefinition"]["taskDefinitionArn"]
        ecs.update_service(cluster=ctx.cluster_arn, service=ctx.service_arn, taskDefinition=registered, forceNewDeployment=True)
        return {"executed_at": _now(), "task_definition": registered, "container": target.get("name"), "image_before": replaced, "image": ctx.image}

    def verify(self, session: Any, ctx: CanaryContext, snapshot: Dict[str, Any], executed: Dict[str, Any], *, timeout_seconds: Optional[int] = None,
               sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> Dict[str, Any]:
        """Steady on the new revision, then the probes for the window, compared with before."""
        ecs = session.client("ecs", region_name=ctx.region)
        started = clock()
        deadline = started + (timeout_seconds or self.verify_timeout_seconds)
        steady_at: Optional[float] = None
        while clock() < deadline:
            service = ecs.describe_services(cluster=ctx.cluster_arn, services=[ctx.service_arn])["services"][0]
            deployments = service.get("deployments") or []
            primary = next((d for d in deployments if d.get("status") == "PRIMARY"), None)
            on_new = primary is not None and primary.get("taskDefinition") == executed["task_definition"]
            settled = on_new and len(deployments) == 1 and int(service.get("runningCount") or 0) >= int(service.get("desiredCount") or 0) \
                and str(primary.get("rolloutState") or "COMPLETED") == "COMPLETED"
            if settled:
                steady_at = clock()
                break
            if primary is not None and str(primary.get("rolloutState") or "") == "FAILED":
                return {"ok": False, "reason": "the deployment failed before steady state", "verified_at": _now(), "waited_s": round(clock() - started, 1)}
            sleep(5)
        if steady_at is None:
            return {"ok": False, "reason": "timeout waiting for steady state on the new revision", "verified_at": _now(), "waited_s": round(clock() - started, 1)}
        probes: List[Dict[str, Any]] = []
        window_end = clock() + ctx.minutes * 60
        failed: Optional[Dict[str, Any]] = None
        while True:
            for url in snapshot.get("probe_urls") or []:
                status = self._status(ctx, url)
                expected = (snapshot.get("before") or {}).get(url)
                ok = status is not None and status < 500 and (expected is None or expected >= 500 or status == expected)
                probes.append({"url": url, "status": status, "expected": expected, "ok": ok, "at": _now()})
                if not ok and failed is None:
                    failed = probes[-1]
            if failed or clock() >= window_end:
                break
            sleep(30)
        metrics = self._metrics(session, ctx, snapshot, started)
        result = {"ok": failed is None, "verified_at": _now(), "steady_after_s": round(steady_at - started, 1), "probes": probes[-20:], "probe_count": len(probes),
                  "metrics": metrics}
        if failed:
            result["reason"] = f"probe {failed['url']} answered {failed['status']} (expected {failed['expected'] if failed['expected'] is not None else 'below 500'})"
        elif metrics.get("worse"):
            result.update(ok=False, reason=metrics["worse"])
        return result

    def rollback(self, session: Any, ctx: CanaryContext, snapshot: Dict[str, Any], result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ecs = session.client("ecs", region_name=ctx.region)
        ecs.update_service(cluster=ctx.cluster_arn, service=ctx.service_arn, taskDefinition=snapshot["task_definition"], forceNewDeployment=True)
        return {"supported": True, "rolled_back_at": _now(), "task_definition": snapshot["task_definition"]}

    # --- helpers -------------------------------------------------------------
    def _probe_urls(self, session: Any, ctx: CanaryContext, service: Mapping[str, Any]) -> List[str]:
        urls = list(ctx.probe_urls)
        target_groups = [lb.get("targetGroupArn") for lb in service.get("loadBalancers") or [] if lb.get("targetGroupArn")]
        if target_groups:
            try:
                elb = session.client("elbv2", region_name=ctx.region)
                groups = elb.describe_target_groups(TargetGroupArns=target_groups).get("TargetGroups") or []
                for group in groups:
                    for lb_arn in group.get("LoadBalancerArns") or []:
                        lb = (elb.describe_load_balancers(LoadBalancerArns=[lb_arn]).get("LoadBalancers") or [{}])[0]
                        dns = lb.get("DNSName")
                        if not dns:
                            continue
                        scheme = "https" if str(group.get("Protocol") or "").upper() == "HTTPS" else "http"
                        health = str(group.get("HealthCheckPath") or "/")
                        for path in [health, *ctx.probe_paths]:
                            url = f"{scheme}://{dns}{path if path.startswith('/') else '/' + path}"
                            if url not in urls:
                                urls.append(url)
            except Exception:  # noqa: BLE001 - no load balancer to probe through is "no probes", said in the result
                pass
        return urls

    def _status(self, ctx: CanaryContext, url: str) -> Optional[int]:
        probe = ctx.probe
        if probe is None:
            import requests

            def probe(u: str) -> int:  # type: ignore[misc]
                return requests.get(u, timeout=10, allow_redirects=False).status_code
        try:
            return int(probe(url))
        except Exception:  # noqa: BLE001 - a refused connection is a failed probe
            return None

    def _metrics(self, session: Any, ctx: CanaryContext, snapshot: Dict[str, Any], started: float) -> Dict[str, Any]:
        """5xx and p95 latency of the target group during the canary against the five minutes before; best effort."""
        groups = snapshot.get("target_groups") or []
        if not groups:
            return {"available": False, "reason": "no target group"}
        try:
            from datetime import timedelta

            cw = session.client("cloudwatch", region_name=ctx.region)
            tg_dim = groups[0].split(":", 5)[5]
            now = datetime.now(timezone.utc)
            canary_start = datetime.fromtimestamp(started, tz=timezone.utc)
            queries = []
            for label, (start, end) in (("before", (canary_start - timedelta(minutes=5), canary_start)), ("during", (canary_start, now))):
                queries.append((label, start, end))
            out: Dict[str, Any] = {"available": True}
            for label, start, end in queries:
                data = cw.get_metric_data(StartTime=start, EndTime=end, MetricDataQueries=[
                    {"Id": "errors", "MetricStat": {"Metric": {"Namespace": "AWS/ApplicationELB", "MetricName": "HTTPCode_Target_5XX_Count",
                                                                "Dimensions": [{"Name": "TargetGroup", "Value": tg_dim}]}, "Period": 300, "Stat": "Sum"}},
                    {"Id": "p95", "MetricStat": {"Metric": {"Namespace": "AWS/ApplicationELB", "MetricName": "TargetResponseTime",
                                                             "Dimensions": [{"Name": "TargetGroup", "Value": tg_dim}]}, "Period": 300, "Stat": "p95"}},
                ])
                values = {r["Id"]: (r.get("Values") or [None])[0] for r in data.get("MetricDataResults") or []}
                out[label] = {"errors_5xx": values.get("errors"), "p95_s": values.get("p95")}
            before, during = out.get("before") or {}, out.get("during") or {}
            if (during.get("errors_5xx") or 0) > (before.get("errors_5xx") or 0) + 1:
                out["worse"] = f"5xx went from {before.get('errors_5xx') or 0} to {during.get('errors_5xx')} during the canary"
            elif before.get("p95_s") and during.get("p95_s") and during["p95_s"] > before["p95_s"] * 2:
                out["worse"] = f"p95 latency went from {before['p95_s']:.2f}s to {during['p95_s']:.2f}s during the canary"
            return out
        except Exception as exc:  # noqa: BLE001 - metrics are evidence, not the verdict
            return {"available": False, "reason": str(exc)[:200]}


ACTION_CATALOG = {CanaryDeploy.action_type: CanaryDeploy}
_core_actions.register("patch", ACTION_CATALOG)
