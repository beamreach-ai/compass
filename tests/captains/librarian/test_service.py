from datetime import datetime, timezone

from captains.librarian.service import InfraMapLibrarian
from captains.mapper.infra_map_ids import (
    github_file_node_id,
    github_repo_node_id,
    tf_address_node_id,
    tf_root_module_node_id,
    tf_state_file_node_id,
)
from infra_map.models import RelationshipEdge, ResourceNode


class _Store:
    def __init__(self, nodes, edges):
        self._nodes = nodes
        self._edges = edges

    def fetch_graph(self, org_id):
        return {"nodes": self._nodes, "edges": self._edges}


def test_librarian_returns_verified_terraform_location():
    ts = datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc)
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
            attributes={"bucket": "beamreach-tf-states", "key": "coworkers/demo/terraform.tfstate", "s3_uri": "s3://beamreach-tf-states/coworkers/demo/terraform.tfstate"},
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
            attributes={"path": "infra/environments/demo"},
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
        RelationshipEdge(
            "org-1",
            "tfstate-instance://demo",
            tf_address_id,
            "corresponds_to",
            attributes={"tf_root_module_id": tf_root_id, "resource_address": tf_address},
            last_seen=ts,
        ),
        RelationshipEdge("org-1", github_file_id, tf_root_id, "part_of", last_seen=ts),
        RelationshipEdge("org-1", github_file_id, tf_address_id, "declares", last_seen=ts),
        RelationshipEdge("org-1", repo_id, github_file_id, "contains", last_seen=ts),
    ]

    result = InfraMapLibrarian(_Store(nodes, edges)).get_terraform_location("org-1", resource_id)

    assert result["status"] == "verified"
    assert result["answer"]["repo"] == "beamreach/beamreach-coworker"
    assert result["answer"]["file"] == "infra/modules/on-premise/main.tf"
    assert result["answer"]["tf_root_module"] == "infra/environments/demo"
    assert result["answer"]["tf_address"] == tf_address


def test_librarian_matches_resource_by_name_and_wildcard_arn():
    ts = datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc)
    resource_id = "arn:aws:lambda:us-east-1:123456789012:function:public-demo-map-publisher"
    state_file_id = tf_state_file_node_id("beamreach-tf-states", "coworkers/demo/terraform.tfstate")
    tf_root_id = tf_root_module_node_id("beamreach/beamreach-demo-infra", "environments/demo")
    tf_address = "module.demo-services.aws_lambda_function.publisher"
    tf_address_id = tf_address_node_id(tf_root_id, tf_address)
    github_file_id = github_file_node_id("beamreach/beamreach-demo-infra", "modules/demo-services/main.tf")
    repo_id = github_repo_node_id("beamreach/beamreach-demo-infra")

    nodes = [
        ResourceNode("org-1", resource_id, "lambda_function", "123456789012", "us-east-1", "public-demo-map-publisher", last_seen=ts),
        ResourceNode(
            "org-1",
            state_file_id,
            "tf_state_file",
            "",
            "global",
            "terraform.tfstate",
            attributes={"s3_uri": "s3://beamreach-tf-states/coworkers/demo/terraform.tfstate"},
            last_seen=ts,
        ),
        ResourceNode(
            "org-1",
            "tfstate-instance://publisher",
            "tf_state_instance",
            "",
            "global",
            tf_address,
            attributes={
                "resource_address": tf_address,
                "module_path": "module.demo-services",
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
            "environments/demo",
            attributes={"path": "environments/demo"},
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
            "beamreach-demo-infra/modules/demo-services/main.tf",
            attributes={"path": "modules/demo-services/main.tf", "repo": "beamreach/beamreach-demo-infra"},
            last_seen=ts,
        ),
        ResourceNode(
            "org-1",
            repo_id,
            "github_repo",
            "beamreach",
            "global",
            "beamreach/beamreach-demo-infra",
            last_seen=ts,
        ),
    ]
    edges = [
        RelationshipEdge("org-1", state_file_id, "tfstate-instance://publisher", "contains", last_seen=ts),
        RelationshipEdge("org-1", "tfstate-instance://publisher", resource_id, "observes", last_seen=ts),
        RelationshipEdge("org-1", tf_root_id, state_file_id, "writes", last_seen=ts),
        RelationshipEdge(
            "org-1",
            "tfstate-instance://publisher",
            tf_address_id,
            "corresponds_to",
            attributes={"tf_root_module_id": tf_root_id, "resource_address": tf_address},
            last_seen=ts,
        ),
        RelationshipEdge("org-1", github_file_id, tf_root_id, "part_of", last_seen=ts),
        RelationshipEdge("org-1", github_file_id, tf_address_id, "declares", last_seen=ts),
        RelationshipEdge("org-1", repo_id, github_file_id, "contains", last_seen=ts),
    ]

    librarian = InfraMapLibrarian(_Store(nodes, edges))

    by_name = librarian.get_terraform_location("org-1", "public-demo-map-publisher")
    by_wildcard_arn = librarian.get_terraform_location(
        "org-1",
        "arn:aws:lambda:us-east-1:*:function:public-demo-map-publisher",
    )

    assert by_name["status"] == "verified"
    assert by_name["answer"]["repo"] == "beamreach/beamreach-demo-infra"
    assert by_wildcard_arn["status"] == "verified"
    assert by_wildcard_arn["answer"]["file"] == "modules/demo-services/main.tf"
