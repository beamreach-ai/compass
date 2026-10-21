import logging
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

from compass_core import sessions
from compass_core.events import current_job_id, emit_job_event
from infra_map.fact_store import InfraMapFactStore
from infra_map.facts import FactBatch
from infra_map.graph_store import GraphStore, open_store
from infra_map.materializer import InfraMapMaterializer
from infra_map.models import RelationshipEdge, ResourceNode
from infra_map.synthetic import is_synthetic_scope

from .coverage_drift import annotate_coverage, annotate_drift
from .github_mapper import GitHubGraphCollector
from .ownership_resolver import (
    build_derived_correspondence_edge_facts,
    build_derived_correspondence_edges,
)
from .tf_state_graph import TerraformStateGraphCollector

logger = logging.getLogger("aws-infra-mapper")
TF_STATE_FILE_PREFIX = "tfstate://s3/"


def _chunked(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def _parse_arn(arn: str) -> Dict[str, str]:
    parts = arn.split(":")
    if len(parts) < 6:
        return {}
    return {
        "partition": parts[1],
        "service": parts[2],
        "region": parts[3],
        "account": parts[4],
        "resource": ":".join(parts[5:]),
    }


def _parse_tf_state_file_id(resource_id: str) -> Optional[Dict[str, str]]:
    if not isinstance(resource_id, str) or not resource_id.startswith(TF_STATE_FILE_PREFIX):
        return None
    bucket_and_key = resource_id[len(TF_STATE_FILE_PREFIX) :]
    bucket, separator, key = bucket_and_key.partition("/")
    if not separator or not bucket or not key:
        return None
    return {"bucket": bucket, "key": key}


def scan_scopes(store: GraphStore, org_id: str, nodes: Sequence[ResourceNode], edges: Sequence[RelationshipEdge]) -> Optional[List[Tuple[str, str]]]:
    """The ``(source, scope)`` pairs a full scan closes within: every pair it produced and every
    pair already on the map, except the seeded synthetic world (``infra_map.synthetic``), which
    no scan produces and none may close. A store without ``scopes`` gets the whole org, as before."""
    pairs = {(n.source or "", n.scope or "") for n in nodes} | {(e.source or "", e.scope or "") for e in edges}
    existing = getattr(store, "scopes", None)
    if existing is None:
        return None
    pairs |= {pair for pair in existing(org_id) if not is_synthetic_scope(pair[1])}
    return sorted(pairs)


def extract_task_definition_secret_refs(container_definitions: Optional[List[dict]]) -> List[Tuple[str, str, Optional[str]]]:
    """
    Return tuples of (secret_arn, container_name, env_var) for every secret reference in the task definition.
    """
    refs: List[Tuple[str, str, Optional[str]]] = []
    if not container_definitions:
        return refs

    for container in container_definitions:
        container_name = container.get("name", "")
        for secret in container.get("secrets", []):
            secret_arn = secret.get("valueFrom")
            if not secret_arn:
                continue
            env_name = secret.get("name")
            refs.append((secret_arn, container_name, env_name))
    return refs


class AWSInfraMapper:
    def __init__(
        self,
        org_id: str,
        workspace_id: str,
        target_session: boto3.Session,
        control_session: boto3.Session,
        table_name: Optional[str] = None,
        *,
        regions: Optional[Sequence[str]] = None,
        collector_cls=None,
        store: Optional[GraphStore] = None,
        fact_store: Optional[InfraMapFactStore] = None,
        github_config: Optional[Dict] = None,
        github_collector_cls=None,
        state_buckets: Optional[Sequence[dict]] = None,
        tf_state_collector_cls=None,
        materializer_cls=None,
    ):
        self.org_id = org_id
        self.workspace_id = workspace_id
        self.target_session = target_session
        self.regions = list(regions) if regions else []
        self.collector_cls = collector_cls or AWSGraphCollector
        self.store = store or open_store({"dynamodb_config": {"map": table_name}}, workspace_id=workspace_id, session=control_session)
        self.github_config = github_config or {}
        self.github_collector_cls = github_collector_cls or GitHubGraphCollector
        self.state_buckets = list(state_buckets or [])
        self.tf_state_collector_cls = tf_state_collector_cls or TerraformStateGraphCollector
        self.fact_store = fact_store
        if self.fact_store is None and table_name and (self.state_buckets or self._should_collect_github()):
            self.fact_store = InfraMapFactStore(
                table_name=table_name,
                workspace_id=workspace_id,
                session=control_session,
            )
        self.materializer = (materializer_cls or InfraMapMaterializer)()

    def run(self) -> None:
        emit_job_event("Resolving target AWS account", event_type="progress", status="running")
        account_id = self._resolve_account_id()
        if not account_id:
            logger.error("❌ Unable to determine target AWS account id; skipping infra map stage.")
            raise RuntimeError("Unable to determine target AWS account id for infra scan")

        emit_job_event("Resolving AWS regions", event_type="progress", status="running")
        regions = self._resolve_regions()
        if not regions:
            logger.error("❌ Unable to resolve AWS regions to scan; skipping infra map stage.")
            raise RuntimeError("Unable to resolve AWS regions for infra scan")
        collector = self.collector_cls(
            org_id=self.org_id,
            account_id=account_id,
            target_session=self.target_session,
            regions=regions,
        )
        emit_job_event(
            "Collecting AWS infrastructure inventory",
            event_type="progress",
            status="running",
            data={"region_count": len(regions)},
        )
        nodes, edges = collector.collect()
        emit_job_event(
            "AWS inventory collection complete",
            event_type="progress",
            status="running",
            data={"node_count": len(nodes), "edge_count": len(edges)},
        )
        emit_job_event("Collecting GitHub ownership facts", event_type="progress", status="running")
        github_nodes, github_edges = self._collect_github_nodes(existing_nodes=nodes)
        if github_nodes:
            nodes.extend(github_nodes)
        if github_edges:
            edges.extend(github_edges)
        emit_job_event(
            "GitHub ownership collection complete",
            event_type="progress",
            status="running",
            data={"node_count": len(github_nodes), "edge_count": len(github_edges)},
        )
        discovered_state_files = self._discover_tf_state_files(github_nodes, github_edges)
        emit_job_event(
            "Collecting Terraform state facts",
            event_type="progress",
            status="running",
            data={"state_file_count": len(discovered_state_files)},
        )
        tf_nodes, tf_edges = self._collect_tf_state_nodes(
            existing_nodes=nodes,
            state_files=discovered_state_files,
        )
        if tf_nodes:
            nodes.extend(tf_nodes)
        if tf_edges:
            edges.extend(tf_edges)
        emit_job_event(
            "Terraform state collection complete",
            event_type="progress",
            status="running",
            data={
                "state_file_count": len(discovered_state_files),
                "node_count": len(tf_nodes),
                "edge_count": len(tf_edges),
            },
        )
        emit_job_event("Deriving ownership correspondence", event_type="progress", status="running")
        self._persist_derived_correspondence_facts(nodes, edges)
        derived_edges = build_derived_correspondence_edges(self.org_id, nodes, edges)
        if derived_edges:
            edges.extend(derived_edges)
        emit_job_event("Computing IAC coverage and drift", event_type="progress", status="running")
        annotate_coverage(nodes, edges)
        annotate_drift(nodes, edges)
        emit_job_event(
            "Writing infra map graph",
            event_type="progress",
            status="running",
            data={"node_count": len(nodes), "edge_count": len(edges)},
        )

        # a full scan: everything the scan did not produce is stale, within the scopes it covers
        with self.store.begin_scan(self.org_id, scopes=scan_scopes(self.store, self.org_id, nodes, edges), job_id=current_job_id()) as scan:
            scan.put_nodes(nodes)
            scan.put_edges(edges)
        logger.info("🌐 Infra map write complete: %d nodes, %d edges", len(nodes), len(edges))
        emit_job_event(
            "Infra map graph write complete",
            event_type="progress",
            status="running",
            data={"node_count": len(nodes), "edge_count": len(edges)},
        )

    def _resolve_account_id(self) -> Optional[str]:
        attempts = []
        for region in (None, "us-east-1"):
            try:
                client_kwargs = {}
                if region:
                    client_kwargs["region_name"] = region
                sts = self.target_session.client("sts", **client_kwargs)
                return sts.get_caller_identity()["Account"]
            except (BotoCoreError, ClientError, EndpointConnectionError, KeyError) as exc:
                attempts.append(f"{region or 'default'}:{exc}")
        if attempts:
            logger.error("Failed to fetch AWS account id via target session: %s", " | ".join(attempts))
        return None

    def _resolve_regions(self) -> List[str]:
        preferred = list(dict.fromkeys(self.regions or []))
        discovered = self._discover_regions()

        merged: List[str] = []
        if preferred:
            merged.extend(preferred)
        for region in discovered:
            if region not in merged:
                merged.append(region)

        if merged:
            if preferred and discovered:
                logger.info(
                    "🌎 Scanning %d preferred regions plus %d auto-discovered regions.",
                    len(preferred),
                    len([r for r in discovered if r not in preferred]),
                )
            return merged

        fallback = self.target_session.region_name or "us-east-1"
        logger.warning("⚠️ Falling back to single region %s for mapper scan.", fallback)
        return [fallback]

    def _collect_github_nodes(
        self,
        existing_nodes: Sequence[ResourceNode],
    ) -> Tuple[List[ResourceNode], List[RelationshipEdge]]:
        if not self._should_collect_github():
            return [], []
        try:
            collector = self.github_collector_cls(
                org_id=self.org_id,
                github_config=self.github_config,
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("⚠️ Could not initialize GitHub collector: %s", exc)
            raise RuntimeError(f"Could not initialize GitHub collector: {exc}") from exc
        try:
            if not hasattr(collector, "collect_fact_batches"):
                return collector.collect(existing_nodes=existing_nodes)

            nodes: Dict[str, ResourceNode] = {}
            edges: Dict[Tuple[str, str, str], RelationshipEdge] = {}
            for batch in collector.collect_fact_batches(existing_nodes=existing_nodes):
                if self.fact_store is not None:
                    self.fact_store.replace_scope(
                        self.org_id,
                        source=batch.source,
                        scope=batch.scope,
                        node_facts=batch.node_facts,
                        edge_facts=batch.edge_facts,
                    )
                materialized_nodes, materialized_edges = self.materializer.materialize_batch(batch)
                for node in materialized_nodes:
                    nodes[node.resource_id] = node
                for edge in materialized_edges:
                    edges[(edge.from_resource_id, edge.to_resource_id, edge.relationship_type)] = edge
            return list(nodes.values()), list(edges.values())
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("⚠️ GitHub ownership collection failed: %s", exc)
            raise RuntimeError(f"GitHub ownership collection failed: {exc}") from exc

    def _collect_tf_state_nodes(
        self,
        existing_nodes: Sequence[ResourceNode],
        *,
        state_files: Optional[Sequence[dict]] = None,
    ) -> Tuple[List[ResourceNode], List[RelationshipEdge]]:
        if not self.tf_state_collector_cls:
            return [], []
        resolved_state_files = list(state_files or [])
        if not resolved_state_files and not self.state_buckets:
            return [], []
        try:
            collector = self.tf_state_collector_cls(
                org_id=self.org_id,
                target_session=self.target_session,
                state_files=resolved_state_files,
                state_buckets=self.state_buckets,
            )
            if not hasattr(collector, "collect_fact_batches"):
                return collector.collect(existing_nodes=existing_nodes)

            nodes: Dict[str, ResourceNode] = {}
            edges: Dict[Tuple[str, str, str], RelationshipEdge] = {}
            for batch in collector.collect_fact_batches(existing_nodes=existing_nodes):
                if self.fact_store is not None:
                    self.fact_store.replace_scope(
                        self.org_id,
                        source=batch.source,
                        scope=batch.scope,
                        node_facts=batch.node_facts,
                        edge_facts=batch.edge_facts,
                    )
                materialized_nodes, materialized_edges = self.materializer.materialize_batch(batch)
                for node in materialized_nodes:
                    nodes[node.resource_id] = node
                for edge in materialized_edges:
                    edges[(edge.from_resource_id, edge.to_resource_id, edge.relationship_type)] = edge
            return list(nodes.values()), list(edges.values())
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("⚠️ Terraform state collection failed: %s", exc)
            raise RuntimeError(f"Terraform state collection failed: {exc}") from exc

    def _discover_tf_state_files(
        self,
        nodes: Sequence[ResourceNode],
        edges: Sequence[RelationshipEdge],
    ) -> List[dict]:
        discovered: Dict[Tuple[str, str], dict] = {}

        for edge in edges:
            if edge.relationship_type != "writes":
                continue
            state_file = _parse_tf_state_file_id(edge.to_resource_id)
            if state_file:
                discovered[(state_file["bucket"], state_file["key"])] = state_file

        for node in nodes:
            if node.type != "tf_root_module":
                continue
            backend = (node.attributes or {}).get("backend")
            if not isinstance(backend, dict):
                continue
            bucket = backend.get("bucket")
            key = backend.get("key")
            if isinstance(bucket, str) and isinstance(key, str) and bucket and key:
                discovered[(bucket, key)] = {"bucket": bucket, "key": key}

        return [
            discovered[key]
            for key in sorted(discovered.keys())
        ]

    def _should_collect_github(self) -> bool:
        if not self.github_collector_cls:
            return False
        token = self.github_config.get("token")
        installation_id = self.github_config.get("installation_id")
        return bool(token or installation_id)

    def _persist_derived_correspondence_facts(
        self,
        nodes: Sequence[ResourceNode],
        edges: Sequence[RelationshipEdge],
    ) -> None:
        if self.fact_store is None:
            return
        grouped: Dict[str, List] = {}
        for scope, fact in build_derived_correspondence_edge_facts(self.org_id, nodes, edges):
            grouped.setdefault(scope, []).append(fact)
        for scope, edge_facts in grouped.items():
            batch = FactBatch(
                org_id=self.org_id,
                source="derived",
                scope=scope,
                node_facts=[],
                edge_facts=edge_facts,
            )
            self.fact_store.replace_scope(
                self.org_id,
                source=batch.source,
                scope=batch.scope,
                node_facts=batch.node_facts,
                edge_facts=batch.edge_facts,
            )

    def _discover_regions(self) -> List[str]:
        try:
            ec2 = self.target_session.client("ec2", region_name="us-east-1")
            resp = ec2.describe_regions(AllRegions=True)
            regions = [
                region["RegionName"]
                for region in resp.get("Regions", [])
                if region.get("OptInStatus") in (None, "opt-in-not-required", "opted-in")
            ]
            if regions:
                logger.info("🌎 Auto-discovered %d AWS regions for mapper scan.", len(regions))
            return regions
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not auto-discover AWS regions: %s", exc)
            return []


class AWSGraphCollector:
    def __init__(
        self,
        org_id: str,
        account_id: str,
        target_session: boto3.Session,
        regions: Sequence[str],
    ):
        self.org_id = org_id
        self.account_id = account_id
        self.target_session = target_session
        self.regions = regions
        self.nodes: Dict[str, ResourceNode] = {}
        self.edges: Dict[Tuple[str, str, str], RelationshipEdge] = {}
        self._task_definition_cache: Dict[str, dict] = {}
        # Roles that ECS tasks run as; their policies are what a stolen
        # credential inside the task can use, so only these get their policy
        # documents collected (see _collect_role_policies).
        self._task_role_arns: set = set()
        self._s3_collected = False
        self.lambda_functions: Dict[str, dict] = {}
        self.lambda_by_name: Dict[str, str] = {}
        self._iam_collected = False

    def collect(self) -> Tuple[List[ResourceNode], List[RelationshipEdge]]:
        total = len(self.regions)
        for i, region in enumerate(self.regions, 1):
            try:
                session = self._regional_session(region)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("⚠️ Could not build regional session for %s: %s", region, exc)
                continue

            logger.info("🔁 Scanning AWS region %s for infra map", region)
            # the dashboard's progress bar reads data: which region of how many, and how
            # many resources were found before it (the regions before it are done)
            emit_job_event(
                f"Scanning region {region} ({i}/{total})",
                event_type="progress",
                status="running",
                data={"region": region, "index": i, "total": total, "resources": len(self.nodes)},
            )
            self._collect_region(session, region)

        return list(self.nodes.values()), list(self.edges.values())

    def _collect_region(self, session: boto3.Session, region: str) -> None:
        self._collect_secrets(session, region)
        self._collect_rds(session, region)
        if not self._s3_collected:
            self._collect_s3(session)
            self._s3_collected = True
        self._collect_ecs(session, region)
        self._collect_ecr(session, region)
        self._collect_elbv2(session, region)
        self._collect_ec2(session, region)
        self._collect_lambda(session, region)
        self._collect_cloudwatch_logs(session, region)
        self._collect_dynamodb(session, region)
        self._collect_sqs(session, region)
        self._collect_sns(session, region)
        self._collect_cloudwatch_events(session, region)
        self._collect_cloudwatch_alarms(session, region)
        self._collect_stepfunctions(session, region)
        self._collect_lambda_event_sources(session, region)
        if not self._iam_collected:
            self._collect_iam()
            self._iam_collected = True

    # ------------------------------------------------------------------ collectors
    def _collect_secrets(self, session: boto3.Session, region: str) -> None:
        try:
            client = session.client("secretsmanager")
            paginator = client.get_paginator("list_secrets")
            for page in paginator.paginate():
                for secret in page.get("SecretList", []):
                    tags = {t["Key"]: t["Value"] for t in secret.get("Tags", [])}
                    node = ResourceNode(
                        org_id=self.org_id,
                        resource_id=secret["ARN"],
                        type="secretsmanager_secret",
                        account_id=self.account_id,
                        region=region,
                        name=secret.get("Name", secret["ARN"].split(":")[-1]),
                        tags=tags,
                        attributes={
                            "rotationEnabled": secret.get("RotationEnabled"),
                            "kmsKeyId": secret.get("KmsKeyId"),
                        },
                    )
                    self._add_node(node)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list secrets in %s: %s", region, exc)

    def _collect_ecs(self, session: boto3.Session, region: str) -> None:
        try:
            ecs = session.client("ecs")
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not create ECS client for %s: %s", region, exc)
            return

        try:
            clusters = self._paginate_list(ecs.get_paginator("list_clusters"), "clusterArns")
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list ECS clusters in %s: %s", region, exc)
            return

        if clusters:
            for chunk in _chunked(clusters, 100):
                try:
                    resp = ecs.describe_clusters(clusters=chunk)
                except (BotoCoreError, ClientError) as exc:
                    logger.warning("⚠️ Failed to describe ECS clusters in %s: %s", region, exc)
                    continue
                for cluster in resp.get("clusters", []):
                    node = ResourceNode(
                        org_id=self.org_id,
                        resource_id=cluster["clusterArn"],
                        type="ecs_cluster",
                        account_id=self.account_id,
                        region=region,
                        name=cluster.get("clusterName", cluster["clusterArn"]),
                        tags={},
                        attributes={
                            "registeredContainerInstancesCount": cluster.get("registeredContainerInstancesCount"),
                            "runningTasksCount": cluster.get("runningTasksCount"),
                        },
                    )
                    self._add_node(node)

        for cluster_arn in clusters:
            services = self._list_services_for_cluster(ecs, cluster_arn)
            if not services:
                continue
            for service_chunk in _chunked(services, 10):
                try:
                    resp = ecs.describe_services(cluster=cluster_arn, services=service_chunk, include=["TAGS"])
                except (BotoCoreError, ClientError) as exc:
                    logger.warning("⚠️ Failed to describe ECS services in %s: %s", region, exc)
                    continue
                for service in resp.get("services", []):
                    service_arn = service["serviceArn"]
                    tags = {t["key"]: t["value"] for t in service.get("tags", [])}
                    node = ResourceNode(
                        org_id=self.org_id,
                        resource_id=service_arn,
                        type="ecs_service",
                        account_id=self.account_id,
                        region=region,
                        name=service.get("serviceName", service_arn),
                        tags=tags,
                        attributes={
                            "clusterArn": service.get("clusterArn"),
                            "taskDefinition": service.get("taskDefinition"),
                            "launchType": service.get("launchType"),
                        },
                    )
                    self._add_node(node)
                    if service.get("clusterArn"):
                        self._add_edge(
                            RelationshipEdge(
                                org_id=self.org_id,
                                from_resource_id=service_arn,
                                to_resource_id=service["clusterArn"],
                                relationship_type="member_of",
                                confidence="high",
                                evidence=["ecs:DescribeServices"],
                            )
                        )

                    self._link_service_security_groups(service, region)
                    self._link_service_target_groups(service)
                    self._link_task_definition(ecs, service.get("taskDefinition"), region, service_arn)

    def _collect_ecr(self, session: boto3.Session, region: str) -> None:
        try:
            ecr = session.client("ecr")
            paginator = ecr.get_paginator("describe_repositories")
            for page in paginator.paginate():
                for repository in page.get("repositories", []):
                    arn = repository.get("repositoryArn")
                    if not arn:
                        continue
                    node = ResourceNode(
                        org_id=self.org_id,
                        resource_id=arn,
                        type="ecr_repository",
                        account_id=self.account_id,
                        region=region,
                        name=repository.get("repositoryName", arn.split("/")[-1]),
                        attributes={
                            "repositoryUri": repository.get("repositoryUri"),
                            "imageTagMutability": repository.get("imageTagMutability"),
                            "encryptionType": (repository.get("encryptionConfiguration") or {}).get("encryptionType"),
                            "scanOnPush": (repository.get("imageScanningConfiguration") or {}).get("scanOnPush"),
                        },
                    )
                    self._add_node(node)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list ECR repositories in %s: %s", region, exc)

    def _collect_elbv2(self, session: boto3.Session, region: str) -> None:
        try:
            elbv2 = session.client("elbv2")
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not create ELBv2 client for %s: %s", region, exc)
            return

        try:
            lb_paginator = elbv2.get_paginator("describe_load_balancers")
            for page in lb_paginator.paginate():
                for lb in page.get("LoadBalancers", []):
                    node = ResourceNode(
                        org_id=self.org_id,
                        resource_id=lb["LoadBalancerArn"],
                        type="elbv2_load_balancer",
                        account_id=self.account_id,
                        region=region,
                        name=lb.get("LoadBalancerName", lb["LoadBalancerArn"]),
                        tags={},
                        attributes={
                            "dnsName": lb.get("DNSName"),
                            "scheme": lb.get("Scheme"),
                            "type": lb.get("Type"),
                        },
                    )
                    self._add_node(node)
                    for sg in lb.get("SecurityGroups", []):
                        self._add_edge(
                            RelationshipEdge(
                                org_id=self.org_id,
                                from_resource_id=lb["LoadBalancerArn"],
                                to_resource_id=sg,
                                relationship_type="secured_by",
                                confidence="medium",
                                evidence=["elbv2:DescribeLoadBalancers"],
                            )
                        )
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list load balancers in %s: %s", region, exc)

        try:
            tg_paginator = elbv2.get_paginator("describe_target_groups")
            for page in tg_paginator.paginate():
                for target_group in page.get("TargetGroups", []):
                    node = ResourceNode(
                        org_id=self.org_id,
                        resource_id=target_group["TargetGroupArn"],
                        type="elbv2_target_group",
                        account_id=self.account_id,
                        region=region,
                        name=target_group.get("TargetGroupName", target_group["TargetGroupArn"]),
                        tags={},
                        attributes={
                            "protocol": target_group.get("Protocol"),
                            "port": target_group.get("Port"),
                            "targetType": target_group.get("TargetType"),
                        },
                    )
                    self._add_node(node)
                    for lb_arn in target_group.get("LoadBalancerArns", []):
                        self._add_edge(
                            RelationshipEdge(
                                org_id=self.org_id,
                                from_resource_id=target_group["TargetGroupArn"],
                                to_resource_id=lb_arn,
                                relationship_type="attached_to",
                                confidence="high",
                                evidence=["elbv2:DescribeTargetGroups"],
                            )
                        )
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list target groups in %s: %s", region, exc)

    def _collect_ec2(self, session: boto3.Session, region: str) -> None:
        try:
            ec2 = session.client("ec2")
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not create EC2 client for %s: %s", region, exc)
            return

        try:
            sg_paginator = ec2.get_paginator("describe_security_groups")
            for page in sg_paginator.paginate():
                for sg in page.get("SecurityGroups", []):
                    tags = {t["Key"]: t["Value"] for t in sg.get("Tags", [])}
                    node = ResourceNode(
                        org_id=self.org_id,
                        resource_id=sg["GroupId"],
                        type="ec2_security_group",
                        account_id=self.account_id,
                        region=region,
                        name=sg.get("GroupName", sg["GroupId"]),
                        tags=tags,
                        attributes={"vpcId": sg.get("VpcId")},
                    )
                    self._add_node(node)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list security groups in %s: %s", region, exc)

        try:
            eni_paginator = ec2.get_paginator("describe_network_interfaces")
            for page in eni_paginator.paginate():
                for eni in page.get("NetworkInterfaces", []):
                    node = ResourceNode(
                        org_id=self.org_id,
                        resource_id=eni["NetworkInterfaceId"],
                        type="ec2_network_interface",
                        account_id=self.account_id,
                        region=region,
                        name=eni["NetworkInterfaceId"],
                        tags={},
                        attributes={
                            "subnetId": eni.get("SubnetId"),
                            "status": eni.get("Status"),
                            "interfaceType": eni.get("InterfaceType"),
                        },
                    )
                    self._add_node(node)
                    for group in eni.get("Groups", []):
                        self._add_edge(
                            RelationshipEdge(
                                org_id=self.org_id,
                                from_resource_id=eni["NetworkInterfaceId"],
                                to_resource_id=group["GroupId"],
                                relationship_type="secured_by",
                                confidence="medium",
                                evidence=["ec2:DescribeNetworkInterfaces"],
                            )
                        )
                    attachment = eni.get("Attachment")
                    if attachment and attachment.get("InstanceId"):
                        self._add_edge(
                            RelationshipEdge(
                                org_id=self.org_id,
                                from_resource_id=eni["NetworkInterfaceId"],
                                to_resource_id=attachment["InstanceId"],
                                relationship_type="attached_to",
                                confidence="medium",
                                evidence=["ec2:DescribeNetworkInterfaces"],
                            )
                        )
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list network interfaces in %s: %s", region, exc)

        try:
            instance_paginator = ec2.get_paginator("describe_instances")
            for page in instance_paginator.paginate():
                for reservation in page.get("Reservations", []):
                    for instance in reservation.get("Instances", []):
                        tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
                        name = tags.get("Name") or instance["InstanceId"]
                        node = ResourceNode(
                            org_id=self.org_id,
                            resource_id=instance["InstanceId"],
                            type="ec2_instance",
                            account_id=self.account_id,
                            region=region,
                            name=name,
                            tags=tags,
                            attributes={
                                "instanceType": instance.get("InstanceType"),
                                "state": instance.get("State", {}).get("Name"),
                                "availabilityZone": instance.get("Placement", {}).get("AvailabilityZone"),
                            },
                        )
                        self._add_node(node)
                        for group in instance.get("SecurityGroups", []):
                            self._add_edge(
                                RelationshipEdge(
                                    org_id=self.org_id,
                                    from_resource_id=instance["InstanceId"],
                                    to_resource_id=group["GroupId"],
                                    relationship_type="secured_by",
                                    confidence="medium",
                                    evidence=["ec2:DescribeInstances"],
                                )
                            )
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list EC2 instances in %s: %s", region, exc)

    def _collect_lambda(self, session: boto3.Session, region: str) -> None:
        try:
            client = session.client("lambda")
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not create Lambda client for %s: %s", region, exc)
            return
        try:
            paginator = client.get_paginator("list_functions")
            for page in paginator.paginate():
                for fn in page.get("Functions", []):
                    arn = fn["FunctionArn"]
                    node = ResourceNode(
                        org_id=self.org_id,
                        resource_id=arn,
                        type="lambda_function",
                        account_id=self.account_id,
                        region=region,
                        name=fn["FunctionName"],
                        tags={},
                        attributes={
                            "runtime": fn.get("Runtime"),
                            "handler": fn.get("Handler"),
                            "memorySize": fn.get("MemorySize"),
                            "timeout": fn.get("Timeout"),
                            "role": fn.get("Role"),
                        },
                    )
                    self._add_node(node)
                    self.lambda_functions[arn] = fn
                    self.lambda_by_name[fn["FunctionName"]] = arn
                    role_arn = fn.get("Role")
                    if role_arn:
                        self._add_edge(
                            RelationshipEdge(
                                org_id=self.org_id,
                                from_resource_id=arn,
                                to_resource_id=role_arn,
                                relationship_type="depends_on",
                                confidence="high",
                                evidence=["lambda:ListFunctions"],
                            )
                        )
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list Lambda functions in %s: %s", region, exc)

    def _collect_lambda_event_sources(self, session: boto3.Session, region: str) -> None:
        try:
            client = session.client("lambda")
            paginator = client.get_paginator("list_event_source_mappings")
            for page in paginator.paginate():
                for mapping in page.get("EventSourceMappings", []):
                    source_arn = mapping.get("EventSourceArn")
                    function_arn = mapping.get("FunctionArn")
                    if not source_arn or not function_arn:
                        continue
                    self._ensure_node_from_arn(source_arn, region)
                    self._add_edge(
                        RelationshipEdge(
                            org_id=self.org_id,
                            from_resource_id=source_arn,
                            to_resource_id=function_arn,
                            relationship_type="routes_to",
                            confidence="high",
                            evidence=["lambda:ListEventSourceMappings"],
                        )
                    )
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list Lambda event source mappings in %s: %s", region, exc)

    def _collect_cloudwatch_logs(self, session: boto3.Session, region: str) -> None:
        try:
            logs_client = session.client("logs")
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not create CloudWatch Logs client for %s: %s", region, exc)
            return
        try:
            paginator = logs_client.get_paginator("describe_log_groups")
            for page in paginator.paginate():
                for group in page.get("logGroups", []):
                    name = group["logGroupName"]
                    arn = group.get("arn", f"arn:aws:logs:{region}:{self.account_id}:log-group:{name}")
                    node = ResourceNode(
                        org_id=self.org_id,
                        resource_id=arn,
                        type="cloudwatch_log_group",
                        account_id=self.account_id,
                        region=region,
                        name=name,
                        attributes={
                            "retentionInDays": group.get("retentionInDays"),
                        },
                    )
                    self._add_node(node)
                    if name.startswith("/aws/lambda/"):
                        fn_name = name.split("/aws/lambda/")[-1]
                        function_arn = self.lambda_by_name.get(fn_name)
                        if function_arn:
                            self._add_edge(
                                RelationshipEdge(
                                    org_id=self.org_id,
                                    from_resource_id=function_arn,
                                    to_resource_id=arn,
                                    relationship_type="writes_to",
                                    confidence="high",
                                    evidence=["logs:DescribeLogGroups"],
                                )
                            )
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list CloudWatch log groups in %s: %s", region, exc)

    def _collect_dynamodb(self, session: boto3.Session, region: str) -> None:
        try:
            client = session.client("dynamodb")
            paginator = client.get_paginator("list_tables")
            for page in paginator.paginate():
                table_names = page.get("TableNames", [])
                for name in table_names:
                    try:
                        desc = client.describe_table(TableName=name)["Table"]
                    except (BotoCoreError, ClientError) as exc:
                        logger.warning("⚠️ Could not describe DynamoDB table %s: %s", name, exc)
                        continue
                    arn = desc["TableArn"]
                    node = ResourceNode(
                        org_id=self.org_id,
                        resource_id=arn,
                        type="dynamodb_table",
                        account_id=self.account_id,
                        region=region,
                        name=name,
                        attributes={
                            "streamEnabled": bool(desc.get("StreamSpecification", {}).get("StreamEnabled")),
                            "billingMode": desc.get("BillingModeSummary", {}).get("BillingMode"),
                        },
                    )
                    self._add_node(node)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list DynamoDB tables in %s: %s", region, exc)

    def _collect_sqs(self, session: boto3.Session, region: str) -> None:
        try:
            sqs = session.client("sqs")
            response = sqs.list_queues()
            for queue_url in response.get("QueueUrls", []):
                attrs = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["All"]).get("Attributes", {})
                arn = attrs.get("QueueArn")
                if not arn:
                    continue
                name = queue_url.split("/")[-1]
                node = ResourceNode(
                    org_id=self.org_id,
                    resource_id=arn,
                    type="sqs_queue",
                    account_id=self.account_id,
                    region=region,
                    name=name,
                    attributes={
                        "visibilityTimeout": attrs.get("VisibilityTimeout"),
                        "fifoQueue": attrs.get("FifoQueue"),
                    },
                )
                self._add_node(node)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list SQS queues in %s: %s", region, exc)

    def _collect_sns(self, session: boto3.Session, region: str) -> None:
        try:
            sns = session.client("sns")
            paginator = sns.get_paginator("list_topics")
            for page in paginator.paginate():
                for topic in page.get("Topics", []):
                    topic_arn = topic["TopicArn"]
                    node = ResourceNode(
                        org_id=self.org_id,
                        resource_id=topic_arn,
                        type="sns_topic",
                        account_id=self.account_id,
                        region=region,
                        name=topic_arn.split(":")[-1],
                        attributes={},
                    )
                    self._add_node(node)
                    try:
                        subs_paginator = sns.get_paginator("list_subscriptions_by_topic")
                        for sub_page in subs_paginator.paginate(TopicArn=topic_arn):
                            for sub in sub_page.get("Subscriptions", []):
                                endpoint = sub.get("Endpoint")
                                protocol = sub.get("Protocol")
                                if not endpoint:
                                    continue
                                target_arn = endpoint if endpoint.startswith("arn:") else None
                                if target_arn:
                                    self._ensure_node_from_arn(target_arn, region)
                                    self._add_edge(
                                        RelationshipEdge(
                                            org_id=self.org_id,
                                            from_resource_id=topic_arn,
                                            to_resource_id=target_arn,
                                            relationship_type="routes_to",
                                            confidence="high",
                                            evidence=["sns:ListSubscriptionsByTopic"],
                                            attributes={"protocol": protocol},
                                        )
                                    )
                    except (BotoCoreError, ClientError) as exc:
                        logger.warning("⚠️ Could not list subscriptions for %s: %s", topic_arn, exc)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list SNS topics in %s: %s", region, exc)

    def _collect_cloudwatch_events(self, session: boto3.Session, region: str) -> None:
        try:
            events = session.client("events")
            paginator = events.get_paginator("list_rules")
            for page in paginator.paginate():
                for rule in page.get("Rules", []):
                    arn = rule["Arn"]
                    node = ResourceNode(
                        org_id=self.org_id,
                        resource_id=arn,
                        type="eventbridge_rule",
                        account_id=self.account_id,
                        region=region,
                        name=rule.get("Name", arn),
                        attributes={
                            "eventPattern": rule.get("EventPattern"),
                            "scheduleExpression": rule.get("ScheduleExpression"),
                            "state": rule.get("State"),
                        },
                    )
                    self._add_node(node)
                    try:
                        targets = events.list_targets_by_rule(Rule=rule["Name"]).get("Targets", [])
                        for target in targets:
                            target_arn = target.get("Arn")
                            if not target_arn:
                                continue
                            self._ensure_node_from_arn(target_arn, region)
                            self._add_edge(
                                RelationshipEdge(
                                    org_id=self.org_id,
                                    from_resource_id=arn,
                                    to_resource_id=target_arn,
                                    relationship_type="routes_to",
                                    confidence="medium",
                                    evidence=["events:ListTargetsByRule"],
                                )
                            )
                    except (BotoCoreError, ClientError) as exc:
                        logger.warning("⚠️ Could not list targets for rule %s: %s", rule.get("Name"), exc)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list EventBridge rules in %s: %s", region, exc)

    def _collect_cloudwatch_alarms(self, session: boto3.Session, region: str) -> None:
        try:
            cloudwatch = session.client("cloudwatch")
            paginator = cloudwatch.get_paginator("describe_alarms")
            for page in paginator.paginate():
                for alarm in page.get("MetricAlarms", []):
                    arn = alarm.get("AlarmArn")
                    if not arn:
                        continue
                    node = ResourceNode(
                        org_id=self.org_id,
                        resource_id=arn,
                        type="cloudwatch_alarm",
                        account_id=self.account_id,
                        region=region,
                        name=alarm.get("AlarmName", arn),
                        attributes={
                            "metricName": alarm.get("MetricName"),
                            "namespace": alarm.get("Namespace"),
                            "stateValue": alarm.get("StateValue"),
                        },
                    )
                    self._add_node(node)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list CloudWatch alarms in %s: %s", region, exc)

    def _collect_stepfunctions(self, session: boto3.Session, region: str) -> None:
        try:
            sfn = session.client("stepfunctions")
            paginator = sfn.get_paginator("list_state_machines")
            for page in paginator.paginate():
                for sm in page.get("stateMachines", []):
                    arn = sm["stateMachineArn"]
                    node = ResourceNode(
                        org_id=self.org_id,
                        resource_id=arn,
                        type="stepfunctions_state_machine",
                        account_id=self.account_id,
                        region=region,
                        name=sm.get("name", arn),
                        attributes={
                            "type": sm.get("type"),
                        },
                    )
                    self._add_node(node)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list Step Functions in %s: %s", region, exc)

    def _collect_iam(self) -> None:
        try:
            iam = self.target_session.client("iam")
            paginator = iam.get_paginator("list_roles")
            for page in paginator.paginate():
                for role in page.get("Roles", []):
                    arn = role["Arn"]
                    node = ResourceNode(
                        org_id=self.org_id,
                        resource_id=arn,
                        type="iam_role",
                        account_id=self.account_id,
                        region="global",
                        name=role.get("RoleName", arn),
                        attributes={
                            "path": role.get("Path"),
                            "maxSessionDuration": role.get("MaxSessionDuration"),
                        },
                    )
                    self._add_node(node)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list IAM roles: %s", exc)
        self._collect_role_policies()

    def _collect_role_policies(self) -> None:
        """Inline and attached policy documents for the roles ECS tasks run as.

        Limited to task roles: that is what a credential harvested inside a
        container can use, and it keeps the call count proportional to the
        number of services rather than the number of roles in the account.
        """
        if not self._task_role_arns:
            return
        try:
            iam = self.target_session.client("iam")
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not create IAM client: %s", exc)
            return
        for role_arn in sorted(self._task_role_arns):
            node = self.nodes.get(role_arn)
            if node is None:
                continue
            role_name = role_arn.rsplit("/", 1)[-1]
            policies: List[dict] = []
            try:
                for page in iam.get_paginator("list_role_policies").paginate(RoleName=role_name):
                    for name in page.get("PolicyNames", []):
                        doc = iam.get_role_policy(RoleName=role_name, PolicyName=name).get("PolicyDocument")
                        policies.append({"name": name, "source": "inline", "document": doc})
                for page in iam.get_paginator("list_attached_role_policies").paginate(RoleName=role_name):
                    for attached in page.get("AttachedPolicies", []):
                        policy_arn = attached.get("PolicyArn")
                        if not policy_arn:
                            continue
                        version = iam.get_policy(PolicyArn=policy_arn)["Policy"].get("DefaultVersionId")
                        doc = iam.get_policy_version(PolicyArn=policy_arn, VersionId=version)["PolicyVersion"].get("Document")
                        policies.append({"name": attached.get("PolicyName"), "source": "managed", "arn": policy_arn, "document": doc})
            except (BotoCoreError, ClientError) as exc:
                logger.warning("⚠️ Could not read policies for %s: %s", role_name, exc)
                node.attributes["policies_error"] = str(exc)
            node.attributes["policies"] = policies

    def _collect_s3(self, session: boto3.Session) -> None:
        """Bucket names and ARNs only (global, listed once)."""
        try:
            s3 = session.client("s3")
            for bucket in s3.list_buckets().get("Buckets", []):
                name = bucket.get("Name")
                if not name:
                    continue
                self._add_node(
                    ResourceNode(
                        org_id=self.org_id,
                        resource_id=f"arn:aws:s3:::{name}",
                        type="s3_bucket",
                        account_id=self.account_id,
                        region="global",
                        name=name,
                        tags={},
                        attributes={"createdAt": str(bucket.get("CreationDate") or "")},
                    )
                )
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list S3 buckets: %s", exc)

    def _collect_rds(self, session: boto3.Session, region: str) -> None:
        """Instance names, ARNs and engines only."""
        try:
            rds = session.client("rds")
            for page in rds.get_paginator("describe_db_instances").paginate():
                for db in page.get("DBInstances", []):
                    arn = db.get("DBInstanceArn")
                    if not arn:
                        continue
                    self._add_node(
                        ResourceNode(
                            org_id=self.org_id,
                            resource_id=arn,
                            type="rds_instance",
                            account_id=self.account_id,
                            region=region,
                            name=db.get("DBInstanceIdentifier", arn),
                            tags={},
                            attributes={
                                "engine": db.get("Engine"),
                                "endpoint": (db.get("Endpoint") or {}).get("Address"),
                                "port": (db.get("Endpoint") or {}).get("Port"),
                                "multiAZ": db.get("MultiAZ"),
                            },
                        )
                    )
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not list RDS instances in %s: %s", region, exc)

    # ------------------------------------------------------------------ helpers
    def _link_service_security_groups(self, service: dict, region: str) -> None:
        security_groups = (
            service.get("networkConfiguration", {})
            .get("awsvpcConfiguration", {})
            .get("securityGroups", [])
        )
        for sg in security_groups:
            self._add_edge(
                RelationshipEdge(
                    org_id=self.org_id,
                    from_resource_id=service["serviceArn"],
                    to_resource_id=sg,
                    relationship_type="secured_by",
                    confidence="medium",
                    evidence=["ecs:DescribeServices"],
                )
            )

    def _link_service_target_groups(self, service: dict) -> None:
        for lb in service.get("loadBalancers", []):
            tg_arn = lb.get("targetGroupArn")
            if not tg_arn:
                continue
            self._add_edge(
                RelationshipEdge(
                    org_id=self.org_id,
                    from_resource_id=service["serviceArn"],
                    to_resource_id=tg_arn,
                    relationship_type="exposes",
                    confidence="high",
                    evidence=["ecs:DescribeServices"],
                )
            )

    def _link_task_definition(
        self,
        ecs_client,
        task_definition_arn: Optional[str],
        region: str,
        service_arn: str,
    ) -> None:
        if not task_definition_arn:
            return
        task_def = self._ensure_task_definition(ecs_client, task_definition_arn, region)
        if not task_def:
            return
        self._add_edge(
            RelationshipEdge(
                org_id=self.org_id,
                from_resource_id=task_definition_arn,
                to_resource_id=service_arn,
                relationship_type="uses_in",
                confidence="high",
                evidence=["ecs:DescribeServices"],
            )
        )
        secret_refs = extract_task_definition_secret_refs(task_def.get("containerDefinitions"))
        for secret_arn, container_name, env_name in secret_refs:
            self._ensure_secret_node(secret_arn, region)
            self._add_edge(
                RelationshipEdge(
                    org_id=self.org_id,
                    from_resource_id=secret_arn,
                    to_resource_id=task_definition_arn,
                    relationship_type="uses_in",
                    confidence="high",
                    evidence=["ecs:DescribeTaskDefinition"],
                    attributes={
                        "container": container_name,
                        "variable": env_name,
                    },
                )
            )

    def _ensure_task_definition(self, ecs_client, arn: str, region: str) -> Optional[dict]:
        if arn in self._task_definition_cache:
            return self._task_definition_cache[arn]
        try:
            resp = ecs_client.describe_task_definition(taskDefinition=arn, include=["TAGS"])
        except (BotoCoreError, ClientError) as exc:
            logger.warning("⚠️ Could not describe task definition %s: %s", arn, exc)
            return None

        task_def = resp.get("taskDefinition")
        if not task_def:
            return None

        tags_list = resp.get("tags", [])
        tags = {t.get("key") or t.get("Key"): t.get("value") or t.get("Value") for t in tags_list if t}

        node = ResourceNode(
            org_id=self.org_id,
            resource_id=arn,
            type="ecs_task_definition",
            account_id=self.account_id,
            region=region,
            name=task_def.get("family", arn),
            tags=tags,
            attributes={
                "revision": task_def.get("revision"),
                "cpu": task_def.get("cpu"),
                "memory": task_def.get("memory"),
                "containers": [c.get("name") for c in task_def.get("containerDefinitions", []) if c.get("name")],
                "taskRoleArn": task_def.get("taskRoleArn"),
                "executionRoleArn": task_def.get("executionRoleArn"),
            },
        )
        self._add_node(node)
        self._task_definition_cache[arn] = task_def
        for role_arn, relationship in (
            (task_def.get("taskRoleArn"), "runs_as"),
            (task_def.get("executionRoleArn"), "started_by"),
        ):
            if not role_arn:
                continue
            self._ensure_role_node(role_arn)
            self._task_role_arns.add(role_arn)
            self._add_edge(
                RelationshipEdge(
                    org_id=self.org_id,
                    from_resource_id=arn,
                    to_resource_id=role_arn,
                    relationship_type=relationship,
                    confidence="high",
                    evidence=["ecs:DescribeTaskDefinition"],
                )
            )
        return task_def

    def _ensure_role_node(self, role_arn: str) -> None:
        if role_arn in self.nodes:
            return
        arn_parts = _parse_arn(role_arn)
        self._add_node(
            ResourceNode(
                org_id=self.org_id,
                resource_id=role_arn,
                type="iam_role",
                account_id=arn_parts.get("account", self.account_id),
                region="global",
                name=arn_parts.get("resource", role_arn).split("/")[-1],
                tags={},
                attributes={"source": "inferred"},
            )
        )

    def _ensure_secret_node(self, secret_arn: str, fallback_region: str) -> None:
        if secret_arn in self.nodes:
            return
        arn_parts = _parse_arn(secret_arn)
        node = ResourceNode(
            org_id=self.org_id,
            resource_id=secret_arn,
            type="secretsmanager_secret",
            account_id=arn_parts.get("account", self.account_id),
            region=arn_parts.get("region", fallback_region),
            name=arn_parts.get("resource", secret_arn).split(":")[-1],
            tags={},
            attributes={"source": "inferred"},
        )
        self._add_node(node)

    def _ensure_node_from_arn(self, arn: str, fallback_region: str) -> None:
        if arn in self.nodes:
            return
        arn_parts = _parse_arn(arn)
        service = arn_parts.get("service")
        region = arn_parts.get("region", fallback_region)
        resource = arn_parts.get("resource", "")
        resource_type = "aws_resource"
        name = resource.split("/")[-1] if resource else arn
        if service == "lambda":
            resource_type = "lambda_function"
        elif service == "sns":
            resource_type = "sns_topic"
        elif service == "sqs":
            resource_type = "sqs_queue"
        elif service == "events":
            resource_type = "eventbridge_rule"
        elif service == "states":
            resource_type = "stepfunctions_state_machine"
        elif service == "dynamodb":
            if resource.startswith("table/") and "/stream/" in resource:
                resource_type = "dynamodb_stream"
                name = resource.split("/stream/")[0].split("/")[-1]
            elif resource.startswith("table/"):
                resource_type = "dynamodb_table"
        node = ResourceNode(
            org_id=self.org_id,
            resource_id=arn,
            type=resource_type,
            account_id=self.account_id,
            region=region,
            name=name,
            attributes={"source": "inferred"},
        )
        self._add_node(node)

    def _add_node(self, node: ResourceNode) -> None:
        if not node.source:
            node.source = "aws_scan"
        if not node.scope:
            node.scope = "aws:full"
        if not node.node_class:
            node.node_class = "identity" if node.type == "iam_role" else "resource"
        self.nodes[node.resource_id] = node

    def _add_edge(self, edge: RelationshipEdge) -> None:
        if not edge.source:
            edge.source = "aws_scan"
        if not edge.scope:
            edge.scope = "aws:full"
        key = (edge.from_resource_id, edge.to_resource_id, edge.relationship_type)
        self.edges[key] = edge

    def _regional_session(self, region: str) -> boto3.Session:
        return sessions.with_region(self.target_session, region)

    def _paginate_list(self, paginator, field: str) -> List[str]:
        results: List[str] = []
        for page in paginator.paginate():
            results.extend(page.get(field, []))
        return results

    def _list_services_for_cluster(self, ecs_client, cluster_arn: str) -> List[str]:
        services: List[str] = []
        paginator = ecs_client.get_paginator("list_services")
        for page in paginator.paginate(cluster=cluster_arn):
            services.extend(page.get("serviceArns", []))
        return services
