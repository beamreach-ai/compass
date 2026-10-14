"""From an alert's source IP to the exact ECS task, and from the task to its
role. Spike A3/A5: a Fargate task's private IP resolves through
``ec2:DescribeNetworkInterfaces`` to its ENI, and the ENI id appears in the
task's attachments in ``ecs:DescribeTasks``; a credential used from inside the
VPC is logged with that same private IP.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("honeypot-correlation")


def _chunks(items: List[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def find_eni_by_ip(ec2, private_ip: str) -> Optional[Dict[str, Any]]:
    resp = ec2.describe_network_interfaces(Filters=[{"Name": "addresses.private-ip-address", "Values": [private_ip]}])
    enis = resp.get("NetworkInterfaces") or []
    if not enis:
        return None
    eni = enis[0]
    return {
        "eni_id": eni.get("NetworkInterfaceId"),
        "description": eni.get("Description"),
        "vpc_id": eni.get("VpcId"),
        "subnet_id": eni.get("SubnetId"),
        "security_groups": [g.get("GroupId") for g in eni.get("Groups", [])],
        "is_ecs_attachment": str(eni.get("Description") or "").startswith("arn:aws:ecs:"),
    }


def find_task_by_eni(ecs, eni_id: str, cluster_arns: Iterable[str]) -> Optional[Dict[str, Any]]:
    """Scan the given clusters' running tasks for the one carrying the ENI."""
    for cluster in cluster_arns:
        arns: List[str] = []
        token = None
        while True:
            kwargs = {"cluster": cluster, "desiredStatus": "RUNNING"}
            if token:
                kwargs["nextToken"] = token
            page = ecs.list_tasks(**kwargs)
            arns.extend(page.get("taskArns") or [])
            token = page.get("nextToken")
            if not token:
                break
        for chunk in _chunks(arns, 100):
            for task in ecs.describe_tasks(cluster=cluster, tasks=chunk).get("tasks", []):
                for att in task.get("attachments", []):
                    details = {d.get("name"): d.get("value") for d in att.get("details", [])}
                    if details.get("networkInterfaceId") == eni_id:
                        return _task_summary(task, cluster, details)
    return None


def _task_summary(task: Dict[str, Any], cluster: str, details: Dict[str, Any]) -> Dict[str, Any]:
    group = task.get("group") or ""
    service_name = group.split(":", 1)[1] if group.startswith("service:") else None
    return {
        "task_arn": task.get("taskArn"),
        "task_id": str(task.get("taskArn") or "").rsplit("/", 1)[-1],
        "cluster_arn": cluster,
        "service_name": service_name,
        "task_definition_arn": task.get("taskDefinitionArn"),
        "containers": [c.get("name") for c in task.get("containers", [])],
        "private_ip": details.get("privateIPv4Address"),
        "started_at": str(task.get("startedAt")) if task.get("startedAt") else None,
        "launch_type": task.get("launchType"),
    }


def task_roles(ecs, task_definition_arn: str) -> Dict[str, Optional[str]]:
    td = ecs.describe_task_definition(taskDefinition=task_definition_arn).get("taskDefinition") or {}
    return {"task_role_arn": td.get("taskRoleArn"), "execution_role_arn": td.get("executionRoleArn")}


def service_arn_for(cluster_arn: str, service_name: Optional[str]) -> Optional[str]:
    if not service_name or not cluster_arn:
        return None
    parts = cluster_arn.split(":", 5)
    if len(parts) < 6 or not parts[5].startswith("cluster/"):
        return None
    cluster_name = parts[5].split("/", 1)[1]
    return f"arn:{parts[1]}:ecs:{parts[3]}:{parts[4]}:service/{cluster_name}/{service_name}"


def correlate(
    session: Any,
    *,
    region: str,
    source_ip: Optional[str],
    cluster_arns: Iterable[str],
    placement: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Everything the incident needs about *where* the trip came from.

    Starts from the placement (the service Compass planted into) and, when the
    alert carries a private IP, pins the exact task. ``cluster_arns`` should be
    the placement's cluster first; other clusters are scanned only when the
    placement does not resolve the task.
    """
    placement = placement or {}
    out: Dict[str, Any] = {
        "source_ip": source_ip,
        "placement_service_arn": placement.get("service_arn"),
        "placement_cluster_arn": placement.get("cluster_arn"),
        "placement_container": placement.get("container"),
        "eni": None,
        "task": None,
        "service_arn": placement.get("service_arn"),
        "task_role_arn": None,
        "execution_role_arn": None,
        "resolved_by": None,
    }
    clusters = [c for c in cluster_arns if c]
    if placement.get("cluster_arn") and placement["cluster_arn"] not in clusters:
        clusters.insert(0, placement["cluster_arn"])
    if not source_ip or session is None:
        return out
    try:
        ec2 = session.client("ec2", region_name=region)
        ecs = session.client("ecs", region_name=region)
        eni = find_eni_by_ip(ec2, source_ip)
        out["eni"] = eni
        if not eni or not eni.get("is_ecs_attachment"):
            return out
        if not clusters:
            clusters = ecs.list_clusters().get("clusterArns", [])
        task = find_task_by_eni(ecs, eni["eni_id"], clusters)
        out["task"] = task
        if not task:
            return out
        out["service_arn"] = service_arn_for(task["cluster_arn"], task.get("service_name")) or out["service_arn"]
        if task.get("task_definition_arn"):
            out.update(task_roles(ecs, task["task_definition_arn"]))
        out["resolved_by"] = "ip->eni->task"
        if out["placement_service_arn"] and out["service_arn"] and out["placement_service_arn"] != out["service_arn"]:
            out["mismatch"] = "the source task is not in the service the honeytoken was planted into"
    except Exception as exc:  # noqa: BLE001 - correlation must never break the incident
        logger.warning("correlation failed for %s: %s", source_ip, exc)
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out
