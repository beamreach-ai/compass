"""A synthetic map the size you ask for, the shape of a real account.

The mix of resource types per thousand resources follows what the collectors in
captains/mapper produce; a few IAM roles and security groups carry most of the
edges (power-law in-degree), because that is what hubs look like in an account
and what a neighbourhood query has to survive. Ownership nodes (repositories,
root modules, state files, state instances) cover `managed_share` of the
resources, with coverage and drift attributes set the way coverage_drift.py
sets them. Everything is deterministic from the seed.

`churn()` is the next day's scan: some resources gone, as many new, a few
changed. Node and edge ids use the collectors' own helpers, so an ownership
question walks the same path it walks on a real map.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from captains.mapper.infra_map_ids import (
    github_repo_node_id,
    tf_root_module_node_id,
    tf_root_scope,
    tf_state_file_node_id,
    tf_state_instance_node_id,
)
from infra_map.models import RelationshipEdge, ResourceNode

REGIONS = ("us-east-1", "eu-west-1", "us-west-2", "ap-southeast-1", "eu-central-1", "ap-northeast-1")
REGION_WEIGHTS = (50, 25, 15, 10, 6, 4)

# per 1000 resources (docs/specs/map-store.md section 10.1)
MIX: Dict[str, int] = {
    "ec2_instance": 150,
    "ec2_network_interface": 150,
    "cloudwatch_log_group": 130,
    "lambda_function": 120,
    "ec2_security_group": 100,
    "iam_role": 60,
    "s3_bucket": 40,
    "sqs_queue": 35,
    "dynamodb_table": 30,
    "secretsmanager_secret": 25,
    "sns_topic": 25,
    "ecs_task_definition": 25,
    "cloudwatch_alarm": 20,
    "ecs_service": 20,
    "ecr_repository": 15,
    "eventbridge_rule": 15,
    "rds_instance": 12,
    "elbv2_target_group": 12,
    "elbv2_load_balancer": 8,
    "stepfunctions_state_machine": 5,
    "ecs_cluster": 3,
}
GLOBAL_TYPES = {"iam_role", "s3_bucket"}
ENVIRONMENTS = ("prod", "staging", "dev", "shared")
TEAMS = ("payments", "search", "identity", "data", "platform", "growth", "ops", "ml")


@dataclass
class WorldSpec:
    resources: int = 10_000
    regions: int = 4
    managed_share: float = 0.6
    drifted_share: float = 0.03
    seed: int = 7
    org_id: str = "bench"
    account: str = "123456789012"
    generation: int = 0  # bumped by churn() so new ids never collide with old ones


@dataclass
class World:
    spec: WorldSpec
    nodes: List[ResourceNode] = field(default_factory=list)
    edges: List[RelationshipEdge] = field(default_factory=list)
    hubs: List[str] = field(default_factory=list)       # the busiest role, security group, ...
    typical: List[str] = field(default_factory=list)    # a median instance, lambda, bucket
    managed: List[str] = field(default_factory=list)    # resources with an observes path to a repo

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    def by_type(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for node in self.nodes:
            out[node.type] = out.get(node.type, 0) + 1
        return out


def _pareto_index(rng: random.Random, n: int, shape: float = 1.16) -> int:
    """An index in [0, n) with a heavy head: shape 1.16 is the 80/20 rule."""
    return min(int(rng.paretovariate(shape)) - 1, n - 1)


def _hex(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def _arn(service: str, region: str, account: str, resource: str) -> str:
    if service in ("iam", "s3"):
        return f"arn:aws:{service}::{account if service == 'iam' else ''}:{resource}"
    return f"arn:aws:{service}:{region}:{account}:{resource}"


def _tags(rng: random.Random, name: str) -> Dict[str, str]:
    return {
        "Name": name,
        "Environment": rng.choice(ENVIRONMENTS),
        "Team": rng.choice(TEAMS),
        "ManagedBy": rng.choice(("terraform", "terraform", "terraform", "console")),
    }


def _node(spec: WorldSpec, rng: random.Random, ntype: str, region: str, service: str, resource: str, name: str,
          attributes: Dict[str, object], observed: datetime) -> ResourceNode:
    return ResourceNode(
        org_id=spec.org_id,
        resource_id=_arn(service, region, spec.account, resource),
        type=ntype,
        account_id=spec.account,
        region=region,
        name=name,
        tags=_tags(rng, name),
        attributes=attributes,
        source="aws_scan",
        scope=spec.account,
        last_seen=observed,
    )


def _edge(spec: WorldSpec, src: str, dst: str, rel: str, observed: datetime, *, source: str = "aws_scan",
          scope: Optional[str] = None, evidence: Optional[List[str]] = None) -> RelationshipEdge:
    return RelationshipEdge(
        org_id=spec.org_id,
        from_resource_id=src,
        to_resource_id=dst,
        relationship_type=rel,
        confidence="high",
        evidence=evidence or [f"{source}:{rel}"],
        source=source,
        scope=scope or spec.account,
        last_seen=observed,
    )


def generate(spec: WorldSpec, *, observed: Optional[datetime] = None) -> World:
    """The world for ``spec``: nodes, edges, and the ids the measurements focus on."""
    rng = random.Random(f"{spec.seed}:{spec.generation}")
    observed = observed or datetime.now(timezone.utc)
    regions = list(REGIONS[: max(1, spec.regions)])
    weights = list(REGION_WEIGHTS[: len(regions)])
    world = World(spec=spec)
    gen = f"g{spec.generation}" if spec.generation else ""

    per_type: Dict[str, List[ResourceNode]] = {t: [] for t in MIX}
    total_weight = sum(MIX.values())
    for ntype, share in MIX.items():
        count = max(1, round(spec.resources * share / total_weight))
        for i in range(count):
            region = "global" if ntype in GLOBAL_TYPES else rng.choices(regions, weights)[0]
            uid = _hex(rng, 12)
            name = f"{rng.choice(TEAMS)}-{ntype.split('_', 1)[-1].replace('_', '-')}-{gen}{i}"
            node = _make(spec, rng, ntype, region, uid, name, observed)
            per_type[ntype].append(node)
            world.nodes.append(node)

    edges = world.edges
    roles = per_type["iam_role"]
    groups = per_type["ec2_security_group"]
    logs = per_type["cloudwatch_log_group"]
    queues = per_type["sqs_queue"]
    topics = per_type["sns_topic"]
    tables = per_type["dynamodb_table"]
    buckets = per_type["s3_bucket"]
    secrets = per_type["secretsmanager_secret"]
    lambdas = per_type["lambda_function"]
    instances = per_type["ec2_instance"]
    data_targets = queues + topics + tables + buckets

    def same_region(pool: Sequence[ResourceNode], region: str) -> List[ResourceNode]:
        chosen = [n for n in pool if n.region == region]
        return chosen or list(pool)

    for i, fn in enumerate(lambdas):
        role = roles[_pareto_index(rng, len(roles))]
        edges.append(_edge(spec, fn.resource_id, role.resource_id, "uses_in", observed))
        for _ in range(rng.choice((0, 1, 1, 2))):
            sg = same_region(groups, fn.region)
            edges.append(_edge(spec, fn.resource_id, sg[_pareto_index(rng, len(sg))].resource_id, "secured_by", observed))
        edges.append(_edge(spec, fn.resource_id, logs[i % len(logs)].resource_id, "writes_to", observed))
        for _ in range(rng.choice((0, 1, 1, 2))):
            edges.append(_edge(spec, fn.resource_id, rng.choice(data_targets).resource_id, "depends_on", observed))

    for inst in instances:
        sg = same_region(groups, inst.region)
        for _ in range(rng.choice((1, 1, 2, 3))):
            edges.append(_edge(spec, inst.resource_id, sg[_pareto_index(rng, len(sg))].resource_id, "secured_by", observed))
        role = roles[_pareto_index(rng, len(roles))]
        edges.append(_edge(spec, inst.resource_id, role.resource_id, "uses_in", observed))
    for i, eni in enumerate(per_type["ec2_network_interface"]):
        owner = instances[i % len(instances)] if i % 4 else lambdas[i % len(lambdas)]
        edges.append(_edge(spec, eni.resource_id, owner.resource_id, "attached_to", observed))
        sg = same_region(groups, eni.region)
        edges.append(_edge(spec, eni.resource_id, sg[_pareto_index(rng, len(sg))].resource_id, "secured_by", observed))

    clusters = per_type["ecs_cluster"]
    task_defs = per_type["ecs_task_definition"]
    target_groups = per_type["elbv2_target_group"]
    for i, svc in enumerate(per_type["ecs_service"]):
        edges.append(_edge(spec, svc.resource_id, clusters[i % len(clusters)].resource_id, "member_of", observed))
        edges.append(_edge(spec, svc.resource_id, task_defs[i % len(task_defs)].resource_id, "uses_in", observed))
        edges.append(_edge(spec, svc.resource_id, target_groups[i % len(target_groups)].resource_id, "exposes", observed))
    for i, td in enumerate(task_defs):
        edges.append(_edge(spec, td.resource_id, roles[_pareto_index(rng, len(roles))].resource_id, "uses_in", observed))
        for _ in range(rng.choice((0, 1, 2))):
            edges.append(_edge(spec, td.resource_id, rng.choice(secrets).resource_id, "uses_in", observed))
        edges.append(_edge(spec, td.resource_id, per_type["ecr_repository"][i % len(per_type["ecr_repository"])].resource_id, "depends_on", observed))
    for i, lb in enumerate(per_type["elbv2_load_balancer"]):
        for tg in target_groups[i::len(per_type["elbv2_load_balancer"])]:
            edges.append(_edge(spec, lb.resource_id, tg.resource_id, "routes_to", observed))
        sg = same_region(groups, lb.region)
        edges.append(_edge(spec, lb.resource_id, sg[_pareto_index(rng, len(sg))].resource_id, "secured_by", observed))
    for topic in topics:
        for q in rng.sample(queues, k=min(len(queues), rng.choice((1, 1, 2)))):
            edges.append(_edge(spec, topic.resource_id, q.resource_id, "routes_to", observed))
    for rule in per_type["eventbridge_rule"]:
        target = rng.choice(lambdas + queues)
        edges.append(_edge(spec, rule.resource_id, target.resource_id, "routes_to", observed))
    for alarm in per_type["cloudwatch_alarm"]:
        edges.append(_edge(spec, alarm.resource_id, rng.choice(instances + lambdas).resource_id, "depends_on", observed))
    for sm in per_type["stepfunctions_state_machine"]:
        for fn in rng.sample(lambdas, k=min(len(lambdas), rng.choice((2, 3, 4)))):
            edges.append(_edge(spec, sm.resource_id, fn.resource_id, "depends_on", observed))
        edges.append(_edge(spec, sm.resource_id, roles[_pareto_index(rng, len(roles))].resource_id, "uses_in", observed))
    for db in per_type["rds_instance"]:
        sg = same_region(groups, db.region)
        edges.append(_edge(spec, db.resource_id, sg[_pareto_index(rng, len(sg))].resource_id, "secured_by", observed))
        edges.append(_edge(spec, rng.choice(secrets).resource_id, db.resource_id, "secured_by", observed))

    _ownership(spec, rng, world, observed)
    world.edges = _dedupe(world.edges)

    world.hubs = [roles[0].resource_id, groups[0].resource_id]
    world.typical = [instances[len(instances) // 2].resource_id, lambdas[len(lambdas) // 2].resource_id, buckets[len(buckets) // 2].resource_id]
    return world


def _dedupe(edges: Iterable[RelationshipEdge]) -> List[RelationshipEdge]:
    """One edge per (from, to, type), as the collectors' dict-keyed edges guarantee."""
    seen: Dict[str, RelationshipEdge] = {}
    for edge in edges:
        seen.setdefault(edge.canonical_edge_id(), edge)
    return list(seen.values())


def _make(spec: WorldSpec, rng: random.Random, ntype: str, region: str, uid: str, name: str, observed: datetime) -> ResourceNode:
    acct = spec.account
    if ntype == "ec2_instance":
        return _node(spec, rng, ntype, region, "ec2", f"instance/i-{uid}", name,
                     {"instanceType": rng.choice(("t3.medium", "m6i.large", "c6g.xlarge", "r6i.2xlarge")), "state": "running",
                      "privateIp": f"10.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}", "imageId": f"ami-{_hex(rng, 17)}",
                      "subnetId": f"subnet-{_hex(rng, 17)}", "vpcId": f"vpc-{_hex(rng, 17)}", "launchTime": observed.isoformat()}, observed)
    if ntype == "ec2_network_interface":
        return _node(spec, rng, ntype, region, "ec2", f"network-interface/eni-{uid}", name,
                     {"status": "in-use", "privateIp": f"10.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}", "subnetId": f"subnet-{_hex(rng, 17)}"}, observed)
    if ntype == "ec2_security_group":
        return _node(spec, rng, ntype, region, "ec2", f"security-group/sg-{uid}", name,
                     {"vpcId": f"vpc-{_hex(rng, 17)}", "ingress": [{"port": p, "cidr": "10.0.0.0/8"} for p in rng.sample((22, 80, 443, 5432, 6379, 8080), k=rng.randint(1, 3))]}, observed)
    if ntype == "lambda_function":
        return _node(spec, rng, ntype, region, "lambda", f"function:{name}", name,
                     {"runtime": rng.choice(("python3.12", "nodejs20.x", "java21")), "memorySize": rng.choice((128, 256, 512, 1024)), "timeout": rng.choice((3, 30, 300)),
                      "handler": "app.handler", "codeSize": rng.randint(1000, 50_000_000), "lastModified": observed.isoformat()}, observed)
    if ntype == "cloudwatch_log_group":
        return _node(spec, rng, ntype, region, "logs", f"log-group:/aws/lambda/{name}", f"/aws/lambda/{name}",
                     {"retentionInDays": rng.choice((7, 14, 30, 90, None)), "storedBytes": rng.randint(0, 10**10)}, observed)
    if ntype == "iam_role":
        return _node(spec, rng, ntype, region, "iam", f"role/{name}", name,
                     {"path": "/", "assumeRolePolicy": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]},
                      "policies": [{"name": "inline-0", "source": "inline", "document": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject", "sqs:SendMessage"], "Resource": "*"}]}}]}, observed)
    if ntype == "s3_bucket":
        return _node(spec, rng, ntype, region, "s3", f"{name}-{uid[:6]}", f"{name}-{uid[:6]}",
                     {"versioning": rng.choice(("Enabled", "Suspended")), "encryption": "AES256", "publicAccessBlocked": True, "creationDate": observed.isoformat()}, observed)
    if ntype == "sqs_queue":
        return _node(spec, rng, ntype, region, "sqs", name, name,
                     {"visibilityTimeout": rng.choice((30, 60, 300)), "fifo": False, "queueUrl": f"https://sqs.{region}.amazonaws.com/{acct}/{name}"}, observed)
    if ntype == "sns_topic":
        return _node(spec, rng, ntype, region, "sns", name, name, {"subscriptions": rng.randint(0, 5)}, observed)
    if ntype == "dynamodb_table":
        return _node(spec, rng, ntype, region, "dynamodb", f"table/{name}", name,
                     {"billingMode": "PAY_PER_REQUEST", "itemCount": rng.randint(0, 10**7), "keySchema": [{"AttributeName": "pk", "KeyType": "HASH"}]}, observed)
    if ntype == "secretsmanager_secret":
        return _node(spec, rng, ntype, region, "secretsmanager", f"secret:{name}-{uid[:6]}", name,
                     {"rotationEnabled": rng.choice((True, False)), "kmsKeyId": f"arn:aws:kms:{region}:{acct}:key/{uuid.UUID(int=rng.getrandbits(128))}"}, observed)
    if ntype == "ecs_cluster":
        return _node(spec, rng, ntype, region, "ecs", f"cluster/{name}", name, {"status": "ACTIVE", "runningTasks": rng.randint(0, 200)}, observed)
    if ntype == "ecs_service":
        return _node(spec, rng, ntype, region, "ecs", f"service/{name}", name, {"desiredCount": rng.randint(1, 8), "launchType": "FARGATE"}, observed)
    if ntype == "ecs_task_definition":
        return _node(spec, rng, ntype, region, "ecs", f"task-definition/{name}:{rng.randint(1, 40)}", name,
                     {"cpu": "512", "memory": "1024", "containerDefinitions": [{"name": "app", "image": f"{acct}.dkr.ecr.{region}.amazonaws.com/{name}:latest",
                      "secrets": [{"name": "DB_PASSWORD", "valueFrom": f"arn:aws:secretsmanager:{region}:{acct}:secret:db-{uid[:6]}"}]}]}, observed)
    if ntype == "ecr_repository":
        return _node(spec, rng, ntype, region, "ecr", f"repository/{name}", name, {"imageCount": rng.randint(1, 500), "scanOnPush": True}, observed)
    if ntype == "elbv2_load_balancer":
        return _node(spec, rng, ntype, region, "elasticloadbalancing", f"loadbalancer/app/{name}/{uid}", name,
                     {"scheme": rng.choice(("internet-facing", "internal")), "type": "application", "dnsName": f"{name}-{uid[:8]}.{region}.elb.amazonaws.com"}, observed)
    if ntype == "elbv2_target_group":
        return _node(spec, rng, ntype, region, "elasticloadbalancing", f"targetgroup/{name}/{uid}", name, {"protocol": "HTTP", "port": 8080, "healthCheckPath": "/health"}, observed)
    if ntype == "rds_instance":
        return _node(spec, rng, ntype, region, "rds", f"db:{name}", name,
                     {"engine": rng.choice(("postgres", "mysql")), "engineVersion": "16.3", "instanceClass": "db.r6g.large", "multiAZ": rng.choice((True, False)),
                      "endpoint": f"{name}.{uid[:10]}.{region}.rds.amazonaws.com"}, observed)
    if ntype == "eventbridge_rule":
        return _node(spec, rng, ntype, region, "events", f"rule/{name}", name, {"scheduleExpression": "rate(5 minutes)", "state": "ENABLED"}, observed)
    if ntype == "cloudwatch_alarm":
        return _node(spec, rng, ntype, region, "cloudwatch", f"alarm:{name}", name, {"metricName": "Errors", "threshold": 1, "state": "OK"}, observed)
    if ntype == "stepfunctions_state_machine":
        return _node(spec, rng, ntype, region, "states", f"stateMachine:{name}", name, {"type": "STANDARD", "status": "ACTIVE"}, observed)
    raise ValueError(ntype)


def _ownership(spec: WorldSpec, rng: random.Random, world: World, observed: datetime) -> None:
    """Repositories, root modules, state files and instances over ``managed_share`` of the
    resources; coverage and drift attributes on every AWS node."""
    aws_nodes = [n for n in world.nodes if n.source == "aws_scan"]
    n_repos = max(1, spec.resources // 2500)
    n_roots = max(1, spec.resources // 200)
    repos = [f"acme/infra-{i}" if i else "acme/infra" for i in range(n_repos)]
    roots: List[Tuple[str, str, str]] = []  # (root_id, repo, path)
    for i in range(n_roots):
        repo = repos[i % n_repos]
        path = f"environments/{rng.choice(ENVIRONMENTS)}/{rng.choice(TEAMS)}-{i}"
        roots.append((tf_root_module_node_id(repo, path), repo, path))

    for repo in repos:
        world.nodes.append(ResourceNode(org_id=spec.org_id, resource_id=github_repo_node_id(repo), type="github_repo", account_id="", region="global",
                                        name=repo, attributes={"default_branch": "main", "full_name": repo}, node_class="artifact",
                                        source="github", scope=f"repo:{repo}", last_seen=observed))
    state_files: List[str] = []
    for root_id, repo, path in roots:
        world.nodes.append(ResourceNode(org_id=spec.org_id, resource_id=root_id, type="tf_root_module", account_id="", region="global", name=path,
                                        attributes={"repo": repo, "path": path, "backend": {"bucket": "acme-tfstate", "key": f"{path}/terraform.tfstate"}},
                                        node_class="artifact", source="github", scope=f"repo:{repo}", last_seen=observed))
        world.edges.append(_edge(spec, root_id, github_repo_node_id(repo), "part_of", observed, source="github", scope=f"repo:{repo}"))
        sf = tf_state_file_node_id("acme-tfstate", f"{path}/terraform.tfstate")
        state_files.append(sf)
        world.nodes.append(ResourceNode(org_id=spec.org_id, resource_id=sf, type="tf_state_file", account_id="", region="global", name="terraform.tfstate",
                                        attributes={"bucket": "acme-tfstate", "key": f"{path}/terraform.tfstate", "serial": rng.randint(1, 900)},
                                        node_class="artifact", source="tfstate", scope=f"state_file:{sf}", last_seen=observed))
        world.edges.append(_edge(spec, root_id, sf, "writes", observed, source="github", scope=tf_root_scope(root_id)))

    managed_count = int(len(aws_nodes) * spec.managed_share)
    managed = rng.sample(aws_nodes, k=managed_count)
    drifted = set(rng.sample(range(managed_count), k=int(managed_count * spec.drifted_share)))
    for i, node in enumerate(managed):
        sf = state_files[i % len(state_files)]
        address = f"module.{node.type}.aws_{node.type}.this"
        inst = tf_state_instance_node_id(sf, address, instance_key=None, aws_resource_id=node.resource_id)
        world.nodes.append(ResourceNode(org_id=spec.org_id, resource_id=inst, type="tf_state_instance", account_id="", region="global", name=address,
                                        attributes={"resource_address": address, "aws_resource_id": node.resource_id, "state_file": sf},
                                        node_class="artifact", source="tfstate", scope=f"state_file:{sf}", last_seen=observed))
        world.edges.append(_edge(spec, sf, inst, "contains", observed, source="tfstate", scope=f"state_file:{sf}"))
        world.edges.append(_edge(spec, inst, node.resource_id, "observes", observed, source="tfstate", scope=f"state_file:{sf}"))
        node.attributes["coverage_score"] = 1
        if i in drifted:
            node.attributes["drift_status"] = "drifted"
            node.attributes["drift_fields"] = [{"field": "tags.Environment", "declared": "prod", "actual": node.tags.get("Environment")}]
        else:
            node.attributes["drift_status"] = "in_sync"
            node.attributes["drift_fields"] = []
        world.managed.append(node.resource_id)
    managed_ids = set(world.managed)
    for node in aws_nodes:
        if node.resource_id not in managed_ids:
            node.attributes["coverage_score"] = 0
            node.attributes["drift_status"] = "no_data"


def churn(world: World, *, removed: float = 0.05, changed: float = 0.02, observed: Optional[datetime] = None) -> World:
    """The next scan of the same account: ``removed`` of the AWS resources are gone (with
    their edges), as many new ones appeared (wired to what is there), and ``changed`` of
    the rest have a different tag or attribute."""
    spec = world.spec
    rng = random.Random(f"{spec.seed}:{spec.generation}:churn")
    observed = observed or datetime.now(timezone.utc)
    aws = [n for n in world.nodes if n.source == "aws_scan"]
    gone = set(n.resource_id for n in rng.sample(aws, k=int(len(aws) * removed)))
    kept_nodes = [n for n in world.nodes if n.resource_id not in gone]
    kept_edges = [e for e in world.edges if e.from_resource_id not in gone and e.to_resource_id not in gone]

    fresh_spec = WorldSpec(**{**spec.__dict__, "resources": max(50, int(len(gone))), "generation": spec.generation + 1})
    fresh = generate(fresh_spec, observed=observed)
    fresh_nodes = [n for n in fresh.nodes if n.source == "aws_scan"]
    fresh_ids = set(n.resource_id for n in fresh_nodes)
    fresh_edges = [e for e in fresh.edges if e.from_resource_id in fresh_ids and e.to_resource_id in fresh_ids]
    # a third of the new resources also point at something that already existed
    existing_targets = [n for n in kept_nodes if n.type in ("iam_role", "ec2_security_group", "sqs_queue", "s3_bucket")]
    for node in fresh_nodes[: len(fresh_nodes) // 3]:
        target = rng.choice(existing_targets)
        rel = "uses_in" if target.type == "iam_role" else "secured_by" if target.type == "ec2_security_group" else "depends_on"
        fresh_edges.append(_edge(spec, node.resource_id, target.resource_id, rel, observed))

    out = World(spec=WorldSpec(**{**spec.__dict__, "generation": spec.generation + 1}))
    out.nodes = kept_nodes + fresh_nodes
    out.edges = _dedupe(kept_edges + fresh_edges)
    for node in out.nodes:
        node.last_seen = observed
    for edge in out.edges:
        edge.last_seen = observed
    for node in rng.sample([n for n in kept_nodes if n.source == "aws_scan"], k=int(len(kept_nodes) * changed)):
        node.tags["Environment"] = "changed-" + node.tags.get("Environment", "")
        node.attributes["lastModified"] = observed.isoformat()
    out.hubs = list(world.hubs)
    out.typical = [t for t in world.typical if t not in gone]
    out.managed = [m for m in world.managed if m not in gone]
    return out


def describe(world: World) -> Dict[str, object]:
    by_type = world.by_type()
    in_degree: Dict[str, int] = {}
    for edge in world.edges:
        in_degree[edge.to_resource_id] = in_degree.get(edge.to_resource_id, 0) + 1
    top = sorted(in_degree.items(), key=lambda kv: -kv[1])[:3]
    return {
        "resources": world.spec.resources,
        "nodes": world.node_count,
        "edges": world.edge_count,
        "edges_per_node": round(world.edge_count / max(1, world.node_count), 2),
        "aws_nodes": sum(1 for n in world.nodes if n.source == "aws_scan"),
        "managed": len(world.managed),
        "types": len(by_type),
        "top_in_degree": [(k.rsplit("/", 1)[-1], v) for k, v in top],
    }
