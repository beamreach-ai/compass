"""HP-08: source IP → ENI → task → role, and the investigate job."""

from __future__ import annotations

from types import SimpleNamespace

from captains.honeypot import incidents
from captains.honeypot.correlation import correlate, find_task_by_eni, service_arn_for
from captains.honeypot.investigate import investigate_incident, load_map_store_for_config

CLUSTER = "arn:aws:ecs:us-east-1:1:cluster/web"
SERVICE = "arn:aws:ecs:us-east-1:1:service/web/web"
TASK = "arn:aws:ecs:us-east-1:1:task/web/abc123"
TD = "arn:aws:ecs:us-east-1:1:task-definition/web:3"
ROLE = "arn:aws:iam::1:role/web-task"
SECRET = "arn:aws:secretsmanager:us-east-1:1:secret:prod/db-x"


class _Ec2:
    def describe_network_interfaces(self, Filters):
        ip = Filters[0]["Values"][0]
        if ip != "10.42.1.49":
            return {"NetworkInterfaces": []}
        return {"NetworkInterfaces": [{"NetworkInterfaceId": "eni-1", "Description": "arn:aws:ecs:us-east-1:1:attachment/x", "VpcId": "vpc-1", "SubnetId": "subnet-1", "Groups": [{"GroupId": "sg-1"}]}]}


class _Ecs:
    def list_clusters(self):
        return {"clusterArns": [CLUSTER]}

    def list_tasks(self, **kw):
        return {"taskArns": ["arn:aws:ecs:us-east-1:1:task/web/other", TASK]}

    def describe_tasks(self, **kw):
        return {"tasks": [
            {"taskArn": "arn:aws:ecs:us-east-1:1:task/web/other", "group": "service:web", "taskDefinitionArn": TD, "containers": [{"name": "web"}],
             "attachments": [{"details": [{"name": "networkInterfaceId", "value": "eni-9"}, {"name": "privateIPv4Address", "value": "10.42.1.9"}]}]},
            {"taskArn": TASK, "group": "service:web", "taskDefinitionArn": TD, "containers": [{"name": "web"}], "launchType": "FARGATE",
             "attachments": [{"details": [{"name": "networkInterfaceId", "value": "eni-1"}, {"name": "privateIPv4Address", "value": "10.42.1.49"}]}]},
        ]}

    def describe_task_definition(self, **kw):
        return {"taskDefinition": {"taskRoleArn": ROLE, "executionRoleArn": "arn:aws:iam::1:role/exec"}}


class _Session:
    def client(self, name, **kw):
        return {"ec2": _Ec2(), "ecs": _Ecs()}[name]


def test_correlate_pins_the_exact_task_and_role():
    out = correlate(_Session(), region="us-east-1", source_ip="10.42.1.49", cluster_arns=[], placement={"service_arn": SERVICE, "cluster_arn": CLUSTER, "container": "web"})
    assert out["eni"]["eni_id"] == "eni-1" and out["eni"]["is_ecs_attachment"]
    assert out["task"]["task_id"] == "abc123" and out["task"]["service_name"] == "web"
    assert out["service_arn"] == SERVICE
    assert out["task_role_arn"] == ROLE
    assert out["resolved_by"] == "ip->eni->task"
    assert "mismatch" not in out


def test_correlate_without_ip_or_unknown_ip():
    out = correlate(_Session(), region="us-east-1", source_ip=None, cluster_arns=[CLUSTER], placement={"service_arn": SERVICE})
    assert out["task"] is None and out["service_arn"] == SERVICE and out["resolved_by"] is None
    out = correlate(_Session(), region="us-east-1", source_ip="85.1.2.3", cluster_arns=[CLUSTER], placement={})
    assert out["eni"] is None and out["task"] is None


def test_mismatch_is_flagged_when_task_is_in_another_service():
    out = correlate(_Session(), region="us-east-1", source_ip="10.42.1.49", cluster_arns=[], placement={"service_arn": "arn:aws:ecs:us-east-1:1:service/web/api", "cluster_arn": CLUSTER})
    assert out["service_arn"] == SERVICE and "mismatch" in out


def test_helpers():
    assert service_arn_for(CLUSTER, "web") == SERVICE
    assert service_arn_for(CLUSTER, None) is None
    assert find_task_by_eni(_Ecs(), "eni-404", [CLUSTER]) is None


class _Table:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[Item["sk"]] = dict(Item)

    def get_item(self, Key):
        return {"Item": self.items[Key["sk"]]} if Key["sk"] in self.items else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames=None, ExpressionAttributeValues=None, **kw):
        item = self.items[Key["sk"]]
        for part in UpdateExpression.replace("SET ", "").split(","):
            k, v = [x.strip() for x in part.split("=")]
            item[ExpressionAttributeNames.get(k, k)] = ExpressionAttributeValues[v]
        return {"Attributes": item}

    def query(self, **kw):
        return {"Items": list(self.items.values())}


class _Store:
    def __init__(self):
        self.nodes = {
            ROLE: SimpleNamespace(resource_id=ROLE, type="iam_role", name="web-task", attributes={"policies": [{"document": {"Statement": [{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": "arn:aws:secretsmanager:us-east-1:1:secret:prod/*"}]}}]}),
            SECRET: SimpleNamespace(resource_id=SECRET, type="secretsmanager_secret", name="prod/db"),
            "arn:aws:s3:::other": SimpleNamespace(resource_id="arn:aws:s3:::other", type="s3_bucket", name="other"),
        }

    def get_node(self, org_id, resource_id, **kw):
        return self.nodes.get(resource_id)

    def fetch_graph(self, org_id, **kw):
        return {"nodes": list(self.nodes.values()), "edges": []}

    def neighborhood(self, org_id, resource_id, **kw):
        return SimpleNamespace(nodes=[], edges=[])


def test_investigate_marks_the_incident(monkeypatch):
    table = _Table()
    pk = "WORKSPACE#ws"
    incident = {"incident_id": "1-abc", "status": "open", "source_ip": "10.42.1.49", "placement": {"service_arn": SERVICE, "cluster_arn": CLUSTER}}
    table.put_item(Item={"pk": pk, "sk": "HONEYPOT#INCIDENT#1-abc", **incident})
    monkeypatch.setattr(incidents, "get_incident", lambda t, p, i: dict(table.items["HONEYPOT#INCIDENT#1-abc"]))
    marked = {}
    monkeypatch.setattr(incidents, "mark", lambda t, p, i, **fields: marked.update(fields))

    out = investigate_incident(table=table, pk=pk, incident_id="1-abc", session=_Session(), region="us-east-1", store=_Store(), org_id="org")
    assert marked["source_task_id"] == "abc123"
    assert marked["correlation"]["task_role_arn"] == ROLE
    assert marked["blast_radius"]["secrets"]["count"] == 1 and marked["blast_radius"]["buckets"]["count"] == 0
    assert marked["blast_radius"]["method"] == "policy-evaluation"
    assert marked["investigation_done_at"]
    assert out["incident_id"] == "1-abc"


def test_investigate_falls_back_to_map_edges_without_policies(monkeypatch):
    table = _Table()
    store = _Store()
    store.nodes[ROLE].attributes = {}
    monkeypatch.setattr(incidents, "get_incident", lambda t, p, i: {"incident_id": "1", "source_ip": None, "placement": {"service_arn": SERVICE}})
    marked = {}
    monkeypatch.setattr(incidents, "mark", lambda t, p, i, **fields: marked.update(fields))
    investigate_incident(table=table, pk="pk", incident_id="1", session=None, region="us-east-1", store=store, org_id="org")
    assert marked["blast_radius"]["method"] == "map-edges"
    assert "source_task_id" not in marked


def test_map_store_loader_from_config():
    store, org = load_map_store_for_config({"dynamodb_config": {}, "org_id": "o"}, "ws")
    assert store is None and org == "o"


def test_region_comes_from_the_placement_arns():
    from captains.honeypot.investigate import region_of_arns
    assert region_of_arns(None, "arn:aws:ecs:us-east-1:1:cluster/web") == "us-east-1"
    assert region_of_arns("", None) is None


def test_live_data_resources_are_used_when_the_map_has_none(monkeypatch):
    from captains.honeypot import investigate as inv

    class _Pager:
        def __init__(self, pages): self.pages = pages
        def paginate(self, **kw): return self.pages

    class _Client:
        def __init__(self, name): self.name = name
        def get_paginator(self, op):
            return _Pager({
                "list_secrets": [{"SecretList": [{"ARN": SECRET, "Name": "prod/db"}]}],
                "describe_db_instances": [{"DBInstances": []}],
                "list_tables": [{"TableNames": []}],
            }[op])
        def list_buckets(self): return {"Buckets": [{"Name": "prod-uploads"}]}

    class _S:
        def client(self, name, **kw): return _Client(name)

    store = _Store()
    del store.nodes[SECRET]
    del store.nodes["arn:aws:s3:::other"]  # map knows no data resources
    monkeypatch.setattr(incidents, "get_incident", lambda t, p, i: {"incident_id": "1", "source_ip": None, "placement": {"service_arn": SERVICE, "cluster_arn": CLUSTER}, "correlation": None})
    monkeypatch.setattr(inv, "correlate", lambda *a, **k: {"task_role_arn": ROLE, "task": {"task_definition_arn": TD}, "service_arn": SERVICE})
    marked = {}
    monkeypatch.setattr(incidents, "mark", lambda t, p, i, **f: marked.update(f))
    inv.investigate_incident(table=_Table(), pk="pk", incident_id="1", session=_S(), region="eu-central-1", store=store, org_id="org")
    assert marked["blast_radius"]["method"] == "policy-evaluation"
    assert marked["blast_radius"]["secrets"]["count"] == 1 and marked["blast_radius"]["buckets"]["count"] == 0
