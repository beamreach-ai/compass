from captains.mapper.aws_mapper import AWSInfraMapper, extract_task_definition_secret_refs
from captains.mapper.infra_map_ids import github_file_node_id, github_repo_node_id, tf_address_node_id, tf_root_module_node_id
from infra_map.facts import EdgeFact, FactBatch, NodeFact
from infra_map.models import RelationshipEdge, ResourceNode


class _FakeSTSClient:
    def get_caller_identity(self):
        return {"Account": "123456789012"}


class _FakeEC2Client:
    def describe_regions(self, AllRegions=True):
        return {"Regions": []}


class _FakeSession:
    region_name = "eu-central-1"

    def client(self, name, **kwargs):
        if name == "sts":
            return _FakeSTSClient()
        if name == "ec2":
            return _FakeEC2Client()
        raise AssertionError(f"unexpected client request for {name}")


class _FakeCollector:
    last_regions = None

    def __init__(self, org_id, account_id, target_session, regions):
        self.org_id = org_id
        self.account_id = account_id
        self.target_session = target_session
        self.regions = regions
        _FakeCollector.last_regions = regions

    def collect(self):
        node = ResourceNode(
            org_id="org-123",
            resource_id="arn:aws:ecs:eu-central-1:123456789012:service/demo",
            type="ecs_service",
            account_id="123456789012",
            region="eu-central-1",
            name="demo",
        )
        edge = RelationshipEdge(
            org_id="org-123",
            from_resource_id="arn:aws:ecs:eu-central-1:123456789012:task-definition/demo:1",
            to_resource_id=node.resource_id,
            relationship_type="uses_in",
            evidence=["test"],
        )
        return [node], [edge]


class _RecordingStore:
    def __init__(self):
        self.nodes = []
        self.edges = []

    def put_resource(self, node):
        self.nodes.append(node)

    def put_relationship(self, edge):
        self.edges.append(edge)

    def fetch_graph(self, org_id):
        return {"nodes": [], "edges": []}

    def delete_resource(self, org_id, resource_id):
        return None

    def delete_relationship(self, edge):
        return None


class _RecordingFactStore:
    def __init__(self):
        self.calls = []

    def replace_scope(self, org_id, *, source, scope, node_facts, edge_facts):
        self.calls.append(
            {
                "org_id": org_id,
                "source": source,
                "scope": scope,
                "node_facts": list(node_facts),
                "edge_facts": list(edge_facts),
            }
        )
        return 0, 0


class _FakeTFStateCollector:
    last_state_files = None
    last_state_buckets = None

    def __init__(self, org_id, target_session, state_files=None, state_buckets=None):
        self.org_id = org_id
        self.target_session = target_session
        self.state_files = list(state_files or [])
        self.state_buckets = list(state_buckets or [])
        _FakeTFStateCollector.last_state_files = self.state_files
        _FakeTFStateCollector.last_state_buckets = self.state_buckets

    def collect_fact_batches(self, *, existing_nodes=()):
        return [
            FactBatch(
                org_id=self.org_id,
                source="tf_state",
                scope="state_file:demo",
                node_facts=[
                    NodeFact(
                        org_id=self.org_id,
                        entity_id="tfstate://s3/tf-states/demo.tfstate",
                        node_type="tf_state_file",
                        account_id="",
                        region="global",
                        name="demo.tfstate",
                    )
                ],
                edge_facts=[
                    EdgeFact(
                        org_id=self.org_id,
                        from_entity_id="tfstate://s3/tf-states/demo.tfstate",
                        to_entity_id="arn:aws:ecs:eu-central-1:123456789012:service/demo",
                        edge_type="observes",
                    )
                ],
            )
        ]


class _FakeTFStateCollectorWithInstances(_FakeTFStateCollector):
    """Also emits a tf_state_instance, which correspondence derivation keys on."""

    def collect_fact_batches(self, *, existing_nodes=()):
        batches = super().collect_fact_batches(existing_nodes=existing_nodes)
        batches[0].node_facts.append(
            NodeFact(
                org_id=self.org_id,
                entity_id="tfstate-instance://demo/module.on-premise.aws_ecs_service.engine",
                node_type="tf_state_instance",
                account_id="",
                region="global",
                name="module.on-premise.aws_ecs_service.engine",
                attributes={
                    "state_file_id": "tfstate://s3/tf-states/demo.tfstate",
                    "resource_address": "module.on-premise.aws_ecs_service.engine",
                },
            )
        )
        return batches


class _FakeGitCollector:
    def __init__(self, org_id, github_config):
        self.org_id = org_id

    def collect_fact_batches(self, *, existing_nodes=()):
        tf_root_id = tf_root_module_node_id("beamreach/platform", "infra/environments/demo")
        tf_address_id = tf_address_node_id(tf_root_id, "module.on-premise.aws_ecs_service.engine")
        file_id = github_file_node_id("beamreach/platform", "infra/modules/on-premise/main.tf")
        repo_id = github_repo_node_id("beamreach/platform")
        return [
            FactBatch(
                org_id=self.org_id,
                source="git_scan",
                scope=f"tf_root_module:{tf_root_id}",
                node_facts=[
                    NodeFact(
                        org_id=self.org_id,
                        entity_id=tf_root_id,
                        node_type="tf_root_module",
                        account_id="beamreach",
                        region="global",
                        name="infra/environments/demo",
                    ),
                    NodeFact(
                        org_id=self.org_id,
                        entity_id=tf_address_id,
                        node_type="tf_address",
                        account_id="",
                        region="global",
                        name="module.on-premise.aws_ecs_service.engine",
                        attributes={"terraform_address": "module.on-premise.aws_ecs_service.engine"},
                    ),
                    NodeFact(
                        org_id=self.org_id,
                        entity_id=file_id,
                        node_type="github_terraform_file",
                        account_id="beamreach",
                        region="global",
                        name="platform/infra/modules/on-premise/main.tf",
                        attributes={"path": "infra/modules/on-premise/main.tf", "repo": "beamreach/platform"},
                    ),
                    NodeFact(
                        org_id=self.org_id,
                        entity_id=repo_id,
                        node_type="github_repo",
                        account_id="beamreach",
                        region="global",
                        name="beamreach/platform",
                    ),
                ],
                edge_facts=[
                    EdgeFact(
                        org_id=self.org_id,
                        from_entity_id=tf_root_id,
                        to_entity_id="tfstate://s3/tf-states/demo.tfstate",
                        edge_type="writes",
                    ),
                    EdgeFact(
                        org_id=self.org_id,
                        from_entity_id=file_id,
                        to_entity_id=tf_root_id,
                        edge_type="part_of",
                    ),
                    EdgeFact(
                        org_id=self.org_id,
                        from_entity_id=file_id,
                        to_entity_id=tf_address_id,
                        edge_type="declares",
                    ),
                    EdgeFact(
                        org_id=self.org_id,
                        from_entity_id=repo_id,
                        to_entity_id=file_id,
                        edge_type="contains",
                    ),
                ],
            )
        ]


def test_extract_task_definition_secret_refs():
    containers = [
        {
            "name": "api",
            "secrets": [
                {"name": "API_SECRET", "valueFrom": "arn:aws:secretsmanager:eu-central-1:123:secret:api"},
                {"name": "DB_SECRET", "valueFrom": "arn:aws:secretsmanager:eu-central-1:123:secret:db"},
            ],
        },
        {
            "name": "worker",
            "secrets": [
                {"name": "WORKER_SECRET", "valueFrom": "arn:aws:secretsmanager:eu-central-1:123:secret:worker"},
            ],
        },
    ]

    refs = extract_task_definition_secret_refs(containers)

    assert ("arn:aws:secretsmanager:eu-central-1:123:secret:api", "api", "API_SECRET") in refs
    assert ("arn:aws:secretsmanager:eu-central-1:123:secret:worker", "worker", "WORKER_SECRET") in refs
    assert len(refs) == 3


def test_aws_infra_mapper_writes_nodes_and_edges():
    store = _RecordingStore()
    target_session = _FakeSession()
    control_session = _FakeSession()

    mapper = AWSInfraMapper(
        org_id="org-123",
        workspace_id="workspace-123",
        target_session=target_session,
        control_session=control_session,
        table_name="beamreach_map_demo",
        regions=["eu-central-1"],
        collector_cls=_FakeCollector,
        store=store,
    )

    mapper.run()

    assert _FakeCollector.last_regions == ["eu-central-1"]
    assert len(store.nodes) == 1
    assert len(store.edges) == 1


def test_aws_infra_mapper_persists_tf_state_facts_before_materializing():
    store = _RecordingStore()
    fact_store = _RecordingFactStore()
    target_session = _FakeSession()
    control_session = _FakeSession()

    mapper = AWSInfraMapper(
        org_id="org-123",
        workspace_id="workspace-123",
        target_session=target_session,
        control_session=control_session,
        table_name="beamreach_map_demo",
        regions=["eu-central-1"],
        collector_cls=_FakeCollector,
        store=store,
        fact_store=fact_store,
        state_buckets=[{"bucket": "tf-states", "prefix": ""}],
        tf_state_collector_cls=_FakeTFStateCollector,
    )

    mapper.run()

    assert len(fact_store.calls) == 1
    call = fact_store.calls[0]
    assert call["source"] == "tf_state"
    assert call["scope"] == "state_file:demo"
    assert len(call["node_facts"]) == 1
    assert len(call["edge_facts"]) == 1
    assert len(store.nodes) == 2
    assert len(store.edges) == 2
    assert _FakeTFStateCollector.last_state_files == []
    assert _FakeTFStateCollector.last_state_buckets == [{"bucket": "tf-states", "prefix": ""}]


def test_aws_infra_mapper_discovers_tf_state_files_from_git_topology():
    store = _RecordingStore()
    fact_store = _RecordingFactStore()
    target_session = _FakeSession()
    control_session = _FakeSession()

    mapper = AWSInfraMapper(
        org_id="org-123",
        workspace_id="workspace-123",
        target_session=target_session,
        control_session=control_session,
        table_name="beamreach_map_demo",
        regions=["eu-central-1"],
        collector_cls=_FakeCollector,
        store=store,
        fact_store=fact_store,
        github_config={"token": "dummy"},
        github_collector_cls=_FakeGitCollector,
        tf_state_collector_cls=_FakeTFStateCollectorWithInstances,
    )

    mapper.run()

    sources = [call["source"] for call in fact_store.calls]
    assert "tf_state" in sources
    assert "git_scan" in sources
    assert "derived" in sources
    derived_calls = [call for call in fact_store.calls if call["source"] == "derived"]
    assert derived_calls
    assert any(edge.edge_type == "corresponds_to" for edge in derived_calls[0]["edge_facts"])
    assert any(edge.relationship_type == "corresponds_to" for edge in store.edges)
    assert _FakeTFStateCollector.last_state_files == [{"bucket": "tf-states", "key": "demo.tfstate"}]
    assert _FakeTFStateCollector.last_state_buckets == []


# ── HP-08: task-definition roles, role policies, S3 and RDS ────────────────

class _FakeEcsForTaskDef:
    def describe_task_definition(self, taskDefinition, include=None):
        return {"taskDefinition": {
            "family": "web", "revision": 3, "cpu": "256", "memory": "512",
            "taskRoleArn": "arn:aws:iam::123456789012:role/web-task",
            "executionRoleArn": "arn:aws:iam::123456789012:role/web-exec",
            "containerDefinitions": [{"name": "web"}, {"name": "sidecar"}],
        }, "tags": []}


class _FakeIam:
    def get_paginator(self, name):
        class P:
            def paginate(self_inner, **kw):
                if name == "list_roles":
                    return [{"Roles": [{"Arn": "arn:aws:iam::123456789012:role/web-task", "RoleName": "web-task"}]}]
                if name == "list_role_policies":
                    return [{"PolicyNames": ["app"]}] if kw["RoleName"] == "web-task" else [{"PolicyNames": []}]
                if name == "list_attached_role_policies":
                    return [{"AttachedPolicies": [{"PolicyArn": "arn:aws:iam::aws:policy/ReadOnly", "PolicyName": "ReadOnly"}]}] if kw["RoleName"] == "web-task" else [{"AttachedPolicies": []}]
                raise AssertionError(name)
        return P()

    def get_role_policy(self, RoleName, PolicyName):
        return {"PolicyDocument": {"Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}]}}

    def get_policy(self, PolicyArn):
        return {"Policy": {"DefaultVersionId": "v2"}}

    def get_policy_version(self, PolicyArn, VersionId):
        assert VersionId == "v2"
        return {"PolicyVersion": {"Document": {"Statement": [{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": "*"}]}}}


class _FakeS3:
    def list_buckets(self):
        return {"Buckets": [{"Name": "prod-uploads"}]}


class _FakeRds:
    def get_paginator(self, name):
        class P:
            def paginate(self_inner, **kw):
                return [{"DBInstances": [{"DBInstanceArn": "arn:aws:rds:eu-central-1:123456789012:db:main", "DBInstanceIdentifier": "main", "Engine": "postgres", "Endpoint": {"Address": "main.x.rds.amazonaws.com", "Port": 5432}}]}]
        return P()


class _FakeTargetSession:
    def client(self, name, **kw):
        return {"iam": _FakeIam(), "s3": _FakeS3(), "rds": _FakeRds()}[name]


def _collector():
    from captains.mapper.aws_mapper import AWSGraphCollector
    return AWSGraphCollector(org_id="org", account_id="123456789012", target_session=_FakeTargetSession(), regions=["eu-central-1"])


def test_task_definition_records_containers_and_role_edges():
    c = _collector()
    td = "arn:aws:ecs:eu-central-1:123456789012:task-definition/web:3"
    c._ensure_task_definition(_FakeEcsForTaskDef(), td, "eu-central-1")
    node = c.nodes[td]
    assert node.attributes["containers"] == ["web", "sidecar"]
    assert node.attributes["taskRoleArn"].endswith("role/web-task")
    assert c.nodes["arn:aws:iam::123456789012:role/web-task"].type == "iam_role"
    assert (td, "arn:aws:iam::123456789012:role/web-task", "runs_as") in c.edges
    assert (td, "arn:aws:iam::123456789012:role/web-exec", "started_by") in c.edges


def test_role_policies_are_collected_only_for_task_roles():
    c = _collector()
    c._ensure_task_definition(_FakeEcsForTaskDef(), "arn:aws:ecs:eu-central-1:123456789012:task-definition/web:3", "eu-central-1")
    c._collect_iam()
    role = c.nodes["arn:aws:iam::123456789012:role/web-task"]
    sources = {p["source"] for p in role.attributes["policies"]}
    assert sources == {"inline", "managed"}
    exec_role = c.nodes["arn:aws:iam::123456789012:role/web-exec"]
    assert exec_role.attributes.get("policies") == [], "execution role has no inline/attached policies in the fake"


def test_s3_and_rds_are_collected_as_names_and_arns():
    c = _collector()
    c._collect_s3(_FakeTargetSession())
    c._collect_rds(_FakeTargetSession(), "eu-central-1")
    assert c.nodes["arn:aws:s3:::prod-uploads"].type == "s3_bucket"
    rds = c.nodes["arn:aws:rds:eu-central-1:123456789012:db:main"]
    assert rds.type == "rds_instance" and rds.attributes["engine"] == "postgres"


def test_each_region_reports_where_the_scan_is_for_the_progress_bar(monkeypatch):
    from captains.mapper.aws_mapper import AWSGraphCollector
    from compass_core import events

    seen = []

    class Sink:
        def append_event(self, job_id, message, *, event_type="info", status=None, data=None, workspace_id=None):
            seen.append((message, data))

    collector = AWSGraphCollector(org_id="org", account_id="123456789012", target_session=None, regions=["us-east-1", "eu-west-1"])
    monkeypatch.setattr(collector, "_regional_session", lambda region: object())
    monkeypatch.setattr(collector, "_collect_region", lambda session, region: collector.nodes.update({f"r-{region}": object()}))
    previous = events.event_sink()
    events.set_event_sink(Sink())
    try:
        with events.job_event_context("job-1", "ws"):
            collector.collect()
    finally:
        events.set_event_sink(previous)
    assert seen == [
        ("Scanning region us-east-1 (1/2)", {"region": "us-east-1", "index": 1, "total": 2, "resources": 0}),
        ("Scanning region eu-west-1 (2/2)", {"region": "eu-west-1", "index": 2, "total": 2, "resources": 1}),
    ]
