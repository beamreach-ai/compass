
from captains.mapper.tf_state_graph import TerraformStateGraphCollector


class _FakeBody:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload


class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return list(self._pages)


class _FakeS3Client:
    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _FakePaginator(
            [
                {
                    "Contents": [
                        {"Key": "envs/demo/terraform.tfstate"},
                    ]
                }
            ]
        )

    def get_object(self, Bucket, Key):
        assert Bucket == "tf-states"
        assert Key == "envs/demo/terraform.tfstate"
        payload = b"""
        {
          "resources": [
            {
              "mode": "managed",
              "type": "aws_ecs_service",
              "name": "engine",
              "module": "module.on-premise",
              "instances": [
                {
                  "attributes": {
                    "arn": "arn:aws:ecs:eu-central-1:123456789012:service/demo/engine",
                    "id": "arn:aws:ecs:eu-central-1:123456789012:service/demo/engine"
                  }
                }
              ]
            }
          ]
        }
        """
        return {"Body": _FakeBody(payload)}


class _FakeSession:
    def client(self, name):
        assert name == "s3"
        return _FakeS3Client()


def test_tf_state_graph_collector_emits_state_and_observation_nodes():
    collector = TerraformStateGraphCollector(
        org_id="org-1",
        target_session=_FakeSession(),
        state_files=[{"bucket": "tf-states", "key": "envs/demo/terraform.tfstate"}],
    )

    nodes, edges = collector.collect(existing_nodes=[])

    node_types = {node.type for node in nodes}
    assert "tf_state_file" in node_types
    assert "tf_state_instance" in node_types
    assert any(node.resource_id == "arn:aws:ecs:eu-central-1:123456789012:service/demo/engine" for node in nodes)

    contains_edges = [edge for edge in edges if edge.relationship_type == "contains"]
    observe_edges = [edge for edge in edges if edge.relationship_type == "observes"]
    assert contains_edges
    assert observe_edges
    assert observe_edges[0].to_resource_id == "arn:aws:ecs:eu-central-1:123456789012:service/demo/engine"


def test_tf_state_graph_collector_emits_fact_batches():
    collector = TerraformStateGraphCollector(
        org_id="org-1",
        target_session=_FakeSession(),
        state_buckets=[{"bucket": "tf-states", "prefix": "envs/"}],
    )

    batches = collector.collect_fact_batches(existing_nodes=[])

    assert len(batches) == 1
    batch = batches[0]
    assert batch.source == "tf_state"
    assert batch.scope.startswith("state_file:tfstate://s3/tf-states/envs/demo/terraform.tfstate")
    assert any(fact.node_type == "tf_state_file" for fact in batch.node_facts)
    assert any(fact.node_type == "tf_state_instance" for fact in batch.node_facts)
    assert any(fact.edge_type == "contains" for fact in batch.edge_facts)
    assert any(fact.edge_type == "observes" for fact in batch.edge_facts)
