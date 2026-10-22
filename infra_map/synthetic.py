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

Two users: the benchmark (tests/benchmark) scales it up and measures the stores;
demo mode (compass_core.demo) seeds it on a live map beside a real account's scan.
Rows carry the mapper's own sources (`aws_scan`, `git_scan`, `tf_state`) so the
coverage and the suggestions treat them as resources; a `WorldSpec.scope` puts the
whole world under one scope (`demo:seed:<n>` for the demo, `world_scope`) so a
real scan, which lists its own scopes, never closes it, and `synthetic=True`
marks every node so the dashboard can say so.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from captains.mapper import software as _software_module
from captains.mapper.infra_map_ids import (
    github_file_node_id,
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

SCOPE_PREFIX = "demo:"


def world_scope(seed: int) -> str:
    """The scope a seeded world lives under: one per seed, so another seed replaces it."""
    return f"{SCOPE_PREFIX}seed:{int(seed)}"


def is_synthetic_scope(scope: Optional[str]) -> bool:
    """Rows a scan may not close: seeded, never scanned."""
    return str(scope or "").startswith(SCOPE_PREFIX)


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
    scope: Optional[str] = None  # one scope for every row (the demo); None: the scopes a real scan would give them
    synthetic: bool = False  # mark every node `synthetic: true` (the demo's detail panel says so)
    repo_owner: str = "acme"  # the GitHub organisation of the synthetic repositories; the demo uses one no real repo has
    edges: int = 0  # at least this many edges: the rules above give about 2.6 per resource; the demo asks for more


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


def _scope(spec: WorldSpec, default: str) -> str:
    return spec.scope or default


def _attributes(spec: WorldSpec, attributes: Dict[str, object]) -> Dict[str, object]:
    return {**attributes, "synthetic": True} if spec.synthetic else attributes


# the images and runtimes a typical account runs, by weight; php2shell's worked example is among them
BASE_IMAGES = ("php:8.3.30-fpm", "php:8.2.29-fpm-alpine3.20", "node:20.17.0-bookworm-slim", "python:3.12.6-slim", "nginx:1.27.1", "eclipse-temurin:21-jre", "ubuntu:24.04")
BREF_LAYER = "arn:aws:lambda:{region}:534081306603:layer:php-83-fpm:51"


def _software(ntype: str, region: str, uid: str, name: str, account: str, attributes: Dict[str, object]) -> None:
    """``software`` (and the image, layer, URL and address attributes the collectors would record)
    on a workload, from its own generator, so adding it moved nothing else in the world."""
    rng = random.Random(f"{uid}:software")
    entries: list = []
    if ntype == "ecs_task_definition":
        image = f"{account}.dkr.ecr.{region}.amazonaws.com/{name}:{rng.randint(10, 60)}"
        attributes["containerDefinitions"][0]["image"] = image  # type: ignore[index]
        base = rng.choice(BASE_IMAGES)
        attributes["images"] = [{"container": "app", "image": image, "digest": "sha256:" + _hex(rng, 64)}]
        attributes["baseImage"] = base
        entries = [e for e in (_software_module.image_software(image, "ecs:DescribeTaskDefinition", "image", digest=attributes["images"][0]["digest"], extra={"container": "app"}),
                               _software_module.image_software(base, "Dockerfile:1", "base", extra={"final": "true", "stage": "0"})) if e]
    elif ntype == "lambda_function":
        fn = {"Runtime": attributes.get("runtime"), "Layers": []}
        if rng.random() < 0.15:
            fn["Runtime"] = "provided.al2"
            fn["Layers"] = [{"Arn": BREF_LAYER.format(region=region)}]
            attributes["runtime"] = "provided.al2"
        attributes["layers"] = [layer["Arn"] for layer in fn["Layers"]]
        attributes["packageType"] = "Zip"
        if rng.random() < 0.1:
            attributes["functionUrl"] = f"https://{_hex(rng, 32)}.lambda-url.{region}.on.aws/"
        entries = _software_module.lambda_software(fn)
    elif ntype == "ec2_instance":
        if rng.random() < 0.2:
            attributes["publicIp"] = f"54.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
        entries = _software_module.instance_software({"ImageId": attributes.get("imageId")})
    elif ntype == "ec2_security_group":
        if rng.random() < 0.3:
            attributes["publicIngress"] = [{"protocol": "tcp", "from_port": 443, "to_port": 443, "cidrs": ["0.0.0.0/0"]}]
    _software_module.put_software(attributes, entries)


def _node(spec: WorldSpec, rng: random.Random, ntype: str, region: str, service: str, resource: str, name: str,
          attributes: Dict[str, object], observed: datetime) -> ResourceNode:
    _software(ntype, region, resource.rsplit("/", 1)[-1].rsplit(":", 1)[-1] if ntype != "ecs_task_definition" else resource, name, spec.account, attributes)
    return ResourceNode(
        org_id=spec.org_id,
        resource_id=_arn(service, region, spec.account, resource),
        type=ntype,
        account_id=spec.account,
        region=region,
        name=name,
        tags=_tags(rng, name),
        attributes=_attributes(spec, attributes),
        source="aws_scan",
        scope=_scope(spec, "aws:full"),
        last_seen=observed,
    )


def _artifact(spec: WorldSpec, resource_id: str, ntype: str, name: str, attributes: Dict[str, object], *, source: str, scope: str,
              observed: datetime) -> ResourceNode:
    """An ownership node (repository, root module, state file, state instance), as the GitHub and state collectors write them."""
    return ResourceNode(org_id=spec.org_id, resource_id=resource_id, type=ntype, account_id="", region="global", name=name,
                        attributes=_attributes(spec, attributes), node_class="artifact", source=source, scope=_scope(spec, scope), last_seen=observed)


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
        scope=_scope(spec, scope or "aws:full"),
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
    _software_module.annotate_exposure(world.nodes, world.edges)
    world.edges = _dedupe(world.edges)
    if spec.edges > len(world.edges):
        _densify(spec, rng, world, per_type, observed)

    world.hubs = [roles[0].resource_id, groups[0].resource_id]
    world.typical = [instances[len(instances) // 2].resource_id, lambdas[len(lambdas) // 2].resource_id, buckets[len(buckets) // 2].resource_id]
    return world


def _densify(spec: WorldSpec, rng: random.Random, world: World, per_type: Dict[str, List[ResourceNode]], observed: datetime) -> None:
    """More of the same kinds of edge until the world has ``spec.edges``: the compute talks to
    more queues, tables, buckets and secrets, sits behind more security groups, writes more
    logs, is watched by more alarms. Deterministic for the seed; stops when the rules are
    exhausted rather than invent a relationship the mapper never produces."""
    compute = per_type["lambda_function"] + per_type["ec2_instance"] + per_type["ecs_task_definition"] + per_type["stepfunctions_state_machine"]
    data = per_type["sqs_queue"] + per_type["sns_topic"] + per_type["dynamodb_table"] + per_type["s3_bucket"]
    rules: List[Tuple[Sequence[ResourceNode], Sequence[ResourceNode], str]] = [
        (compute, data, "depends_on"),
        (compute, per_type["secretsmanager_secret"], "uses_in"),
        (per_type["ec2_instance"] + per_type["ecs_task_definition"], per_type["cloudwatch_log_group"], "writes_to"),
        (per_type["ec2_instance"] + per_type["lambda_function"] + per_type["rds_instance"], per_type["ec2_security_group"], "secured_by"),
        (per_type["cloudwatch_alarm"], compute + per_type["rds_instance"] + per_type["sqs_queue"], "depends_on"),
        (per_type["eventbridge_rule"], per_type["lambda_function"] + per_type["sqs_queue"] + per_type["sns_topic"], "routes_to"),
        (compute, per_type["iam_role"], "uses_in"),
    ]
    rules = [r for r in rules if r[0] and r[1]]
    seen = {(e.from_resource_id, e.to_resource_id, e.relationship_type) for e in world.edges}
    possible = sum(len(a) * len(b) for a, b, _ in rules)
    want = min(spec.edges, len(seen) + possible)
    misses = 0
    while len(seen) < want and misses < 20 * want:
        src_pool, dst_pool, rel = rng.choice(rules)
        src = src_pool[_pareto_index(rng, len(src_pool))] if rng.random() < 0.3 else rng.choice(src_pool)
        dst = dst_pool[_pareto_index(rng, len(dst_pool))]  # hubs stay hubs
        key = (src.resource_id, dst.resource_id, rel)
        if src.resource_id == dst.resource_id or key in seen:
            misses += 1
            continue
        seen.add(key)
        world.edges.append(_edge(spec, src.resource_id, dst.resource_id, rel, observed))


def _dedupe(edges: Iterable[RelationshipEdge]) -> List[RelationshipEdge]:
    """One edge per (from, to, type), as the collectors' dict-keyed edges guarantee."""
    seen: Dict[str, RelationshipEdge] = {}
    for edge in edges:
        seen.setdefault(edge.canonical_edge_id(), edge)
    return list(seen.values())


def _created(rng: random.Random) -> datetime:
    """When a resource was made: from the seed, not the clock, so the same world seeded twice is unchanged."""
    return datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=rng.randint(0, 600 * 86400))


def _make(spec: WorldSpec, rng: random.Random, ntype: str, region: str, uid: str, name: str, observed: datetime) -> ResourceNode:
    acct = spec.account
    if ntype == "ec2_instance":
        return _node(spec, rng, ntype, region, "ec2", f"instance/i-{uid}", name,
                     {"instanceType": rng.choice(("t3.medium", "m6i.large", "c6g.xlarge", "r6i.2xlarge")), "state": "running",
                      "privateIp": f"10.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}", "imageId": f"ami-{_hex(rng, 17)}",
                      "subnetId": f"subnet-{_hex(rng, 17)}", "vpcId": f"vpc-{_hex(rng, 17)}", "launchTime": _created(rng).isoformat()}, observed)
    if ntype == "ec2_network_interface":
        return _node(spec, rng, ntype, region, "ec2", f"network-interface/eni-{uid}", name,
                     {"status": "in-use", "privateIp": f"10.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}", "subnetId": f"subnet-{_hex(rng, 17)}"}, observed)
    if ntype == "ec2_security_group":
        return _node(spec, rng, ntype, region, "ec2", f"security-group/sg-{uid}", name,
                     {"vpcId": f"vpc-{_hex(rng, 17)}", "ingress": [{"port": p, "cidr": "10.0.0.0/8"} for p in rng.sample((22, 80, 443, 5432, 6379, 8080), k=rng.randint(1, 3))]}, observed)
    if ntype == "lambda_function":
        return _node(spec, rng, ntype, region, "lambda", f"function:{name}", name,
                     {"runtime": rng.choice(("python3.12", "nodejs20.x", "java21")), "memorySize": rng.choice((128, 256, 512, 1024)), "timeout": rng.choice((3, 30, 300)),
                      "handler": "app.handler", "codeSize": rng.randint(1000, 50_000_000), "lastModified": _created(rng).isoformat()}, observed)
    if ntype == "cloudwatch_log_group":
        return _node(spec, rng, ntype, region, "logs", f"log-group:/aws/lambda/{name}", f"/aws/lambda/{name}",
                     {"retentionInDays": rng.choice((7, 14, 30, 90, None)), "storedBytes": rng.randint(0, 10**10)}, observed)
    if ntype == "iam_role":
        return _node(spec, rng, ntype, region, "iam", f"role/{name}", name,
                     {"path": "/", "assumeRolePolicy": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]},
                      "policies": [{"name": "inline-0", "source": "inline", "document": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject", "sqs:SendMessage"], "Resource": "*"}]}}]}, observed)
    if ntype == "s3_bucket":
        return _node(spec, rng, ntype, region, "s3", f"{name}-{uid[:6]}", f"{name}-{uid[:6]}",
                     {"versioning": rng.choice(("Enabled", "Suspended")), "encryption": "AES256", "publicAccessBlocked": True, "creationDate": _created(rng).isoformat()}, observed)
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
    repos = [f"{spec.repo_owner}/infra-{i}" if i else f"{spec.repo_owner}/infra" for i in range(n_repos)]
    roots: List[Tuple[str, str, str]] = []  # (root_id, repo, path)
    for i in range(n_roots):
        repo = repos[i % n_repos]
        path = f"environments/{rng.choice(ENVIRONMENTS)}/{rng.choice(TEAMS)}-{i}"
        roots.append((tf_root_module_node_id(repo, path), repo, path))

    # the sources and scopes are the collectors' own: git_scan per repository and per root module,
    # tf_state per state file (captains/mapper/infra_map_ids.py)
    task_defs = [n for n in aws_nodes if n.type == "ecs_task_definition"]
    for i, repo in enumerate(repos):
        world.nodes.append(_artifact(spec, github_repo_node_id(repo), "github_repo", repo, {"default_branch": "main", "full_name": repo},
                                     source="git_scan", scope=f"repo:{repo}", observed=observed))
        # the Dockerfile that builds each service's image, as the GitHub collector would read it
        for td in task_defs[i::n_repos][:40]:
            path = f"services/{td.name}/Dockerfile"
            parsed = _software_module.parse_dockerfile(f"FROM {td.attributes['baseImage']}\n", path=path)
            attributes: Dict[str, object] = {"path": path, "repo": repo, "base_images": parsed.base_images}
            _software_module.put_software(attributes, parsed.software)
            world.nodes.append(_artifact(spec, github_file_node_id(repo, path), "github_dockerfile", f"{repo.split('/')[-1]}/{path}", attributes,
                                         source="git_scan", scope=f"repo:{repo}", observed=observed))
            world.edges.append(_edge(spec, github_repo_node_id(repo), github_file_node_id(repo, path), "contains", observed, source="git_scan", scope=f"repo:{repo}"))
    state_files: List[str] = []
    for root_id, repo, path in roots:
        world.nodes.append(_artifact(spec, root_id, "tf_root_module", path,
                                     {"repo": repo, "path": path, "backend": {"bucket": "acme-tfstate", "key": f"{path}/terraform.tfstate"}},
                                     source="git_scan", scope=tf_root_scope(root_id), observed=observed))
        world.edges.append(_edge(spec, root_id, github_repo_node_id(repo), "part_of", observed, source="git_scan", scope=f"repo:{repo}"))
        sf = tf_state_file_node_id("acme-tfstate", f"{path}/terraform.tfstate")
        state_files.append(sf)
        world.nodes.append(_artifact(spec, sf, "tf_state_file", "terraform.tfstate",
                                     {"bucket": "acme-tfstate", "key": f"{path}/terraform.tfstate", "serial": rng.randint(1, 900)},
                                     source="tf_state", scope=f"state_file:{sf}", observed=observed))
        world.edges.append(_edge(spec, root_id, sf, "writes", observed, source="git_scan", scope=tf_root_scope(root_id)))

    managed_count = int(len(aws_nodes) * spec.managed_share)
    managed = rng.sample(aws_nodes, k=managed_count)
    drifted = set(rng.sample(range(managed_count), k=int(managed_count * spec.drifted_share)))
    for i, node in enumerate(managed):
        sf = state_files[i % len(state_files)]
        address = f"module.{node.type}.aws_{node.type}.this"
        inst = tf_state_instance_node_id(sf, address, instance_key=None, aws_resource_id=node.resource_id)
        world.nodes.append(_artifact(spec, inst, "tf_state_instance", address, {"resource_address": address, "aws_resource_id": node.resource_id, "state_file": sf},
                                     source="tf_state", scope=f"state_file:{sf}", observed=observed))
        world.edges.append(_edge(spec, sf, inst, "contains", observed, source="tf_state", scope=f"state_file:{sf}"))
        world.edges.append(_edge(spec, inst, node.resource_id, "observes", observed, source="tf_state", scope=f"state_file:{sf}"))
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
