from datetime import datetime, timezone

from captains.mapper.infra_map_ids import (
    github_file_node_id,
    github_repo_node_id,
    tf_address_node_id,
    tf_root_module_node_id,
    tf_state_file_node_id,
)
from captains.mapper.ownership_resolver import InfraMapOwnershipResolver
from infra_map.models import RelationshipEdge, ResourceNode


class _Store:
    def __init__(self, nodes, edges):
        self._nodes = nodes
        self._edges = edges

    def fetch_graph(self, org_id):
        return {"nodes": self._nodes, "edges": self._edges}


def test_resolve_ownership_returns_verified_answer():
    ts = datetime(2026, 4, 3, 10, 0, tzinfo=timezone.utc)
    resource_id = "arn:aws:ecs:eu-central-1:123456789012:service/demo/engine"
    state_file_id = tf_state_file_node_id("beamreach-tf-states", "coworkers/demo/terraform.tfstate")
    tf_root_id = tf_root_module_node_id("beamreach/beamreach-coworker", "infra/environments/demo")
    tf_address = "module.on-premise.aws_ecs_service.engine"
    tf_address_id = tf_address_node_id(tf_root_id, tf_address)
    github_file_id = github_file_node_id("beamreach/beamreach-coworker", "infra/modules/on-premise/main.tf")
    repo_id = github_repo_node_id("beamreach/beamreach-coworker")

    nodes = [
        ResourceNode("org-1", resource_id, "ecs_service", "123456789012", "eu-central-1", "engine", last_seen=ts),
        ResourceNode(
            "org-1",
            state_file_id,
            "tf_state_file",
            "",
            "global",
            "terraform.tfstate",
            attributes={"bucket": "beamreach-tf-states", "key": "coworkers/demo/terraform.tfstate"},
            last_seen=ts,
        ),
        ResourceNode(
            "org-1",
            "tfstate-instance://demo",
            "tf_state_instance",
            "",
            "global",
            tf_address,
            attributes={
                "resource_address": tf_address,
                "module_path": "module.on-premise",
                "state_file_id": state_file_id,
                "aws_resource_id": resource_id,
            },
            last_seen=ts,
        ),
        ResourceNode(
            "org-1",
            tf_root_id,
            "tf_root_module",
            "beamreach",
            "global",
            "infra/environments/demo",
            last_seen=ts,
        ),
        ResourceNode(
            "org-1",
            tf_address_id,
            "tf_address",
            "",
            "global",
            tf_address,
            attributes={"terraform_address": tf_address},
            last_seen=ts,
        ),
        ResourceNode(
            "org-1",
            github_file_id,
            "github_terraform_file",
            "beamreach",
            "global",
            "beamreach-coworker/infra/modules/on-premise/main.tf",
            attributes={"path": "infra/modules/on-premise/main.tf", "repo": "beamreach/beamreach-coworker"},
            last_seen=ts,
        ),
        ResourceNode(
            "org-1",
            repo_id,
            "github_repo",
            "beamreach",
            "global",
            "beamreach/beamreach-coworker",
            last_seen=ts,
        ),
    ]
    edges = [
        RelationshipEdge("org-1", state_file_id, "tfstate-instance://demo", "contains", last_seen=ts),
        RelationshipEdge("org-1", "tfstate-instance://demo", resource_id, "observes", last_seen=ts),
        RelationshipEdge("org-1", tf_root_id, state_file_id, "writes", last_seen=ts),
        RelationshipEdge("org-1", "tfstate-instance://demo", tf_address_id, "corresponds_to", last_seen=ts),
        RelationshipEdge("org-1", github_file_id, tf_root_id, "part_of", last_seen=ts),
        RelationshipEdge("org-1", github_file_id, tf_address_id, "declares", last_seen=ts),
        RelationshipEdge("org-1", repo_id, github_file_id, "contains", last_seen=ts),
    ]

    resolver = InfraMapOwnershipResolver(_Store(nodes, edges))
    result = resolver.resolve_ownership("org-1", resource_id)

    assert result["status"] == "verified"
    assert result["answer"]["repo"] == "beamreach/beamreach-coworker"
    assert result["answer"]["file"] == "infra/modules/on-premise/main.tf"
    assert result["answer"]["resource_address"] == tf_address
    assert result["answer"]["module_path"] == "module.on-premise"
