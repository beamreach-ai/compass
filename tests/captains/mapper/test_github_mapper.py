import importlib.util
from datetime import datetime

import pytest

from captains.mapper.github_mapper import GitHubGraphCollector
from infra_map.models import ResourceNode


class _FakeOwner:
    def __init__(self, login: str):
        self.login = login


class _FakeContent:
    def __init__(self, text: str):
        self.decoded_content = text.encode("utf-8")


class _FakeTreeItem:
    def __init__(self, path: str):
        self.path = path
        self.type = "blob"
        self.sha = f"sha-{path}"


class _FakeTree:
    def __init__(self, paths):
        self.tree = [_FakeTreeItem(path) for path in paths]


class _FakeRepo:
    def __init__(self, full_name: str, files):
        owner, name = full_name.split("/", 1)
        self.full_name = full_name
        self.name = name
        self.owner = _FakeOwner(owner)
        self.private = False
        self.archived = False
        self.default_branch = "main"
        self.html_url = f"https://github.com/{full_name}"
        self.language = "Python"
        self.pushed_at = datetime(2024, 1, 1)
        self._files = files

    def get_git_tree(self, branch, recursive=True):
        assert branch == "main"
        return _FakeTree(self._files.keys())

    def get_contents(self, path, ref):
        assert ref == "main"
        return _FakeContent(self._files[path])


class _FakeUser:
    def __init__(self, repos):
        self._repos = repos

    def get_repos(self):
        return self._repos


class _FakeGithub:
    def __init__(self, repos):
        self._user = _FakeUser(repos)

    def get_user(self):
        return self._user


def _aws_ecr_node() -> ResourceNode:
    return ResourceNode(
        org_id="org-1",
        resource_id="arn:aws:ecr:us-east-1:123456789012:repository/api",
        type="ecr_repository",
        account_id="123456789012",
        region="us-east-1",
        name="api",
        attributes={"repositoryUri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/api"},
    )


def test_github_collector_links_workflow_to_ecr():
    files = {
        ".github/workflows/build.yml": """
name: Build
on: push
jobs:
  build:
    steps:
      - run: |
          docker build -t 123456789012.dkr.ecr.us-east-1.amazonaws.com/api:latest .
          docker push 123456789012.dkr.ecr.us-east-1.amazonaws.com/api:latest
""",
        "Dockerfile": "FROM public.ecr.aws/docker/library/python:3.11\n",
    }
    repo = _FakeRepo("beamreach/api", files)
    github = _FakeGithub([repo])
    collector = GitHubGraphCollector(
        org_id="org-1",
        github_config={"token": "dummy-token", "orgs": ["beamreach"]},
        github_client=github,
    )

    nodes, edges = collector.collect(existing_nodes=[_aws_ecr_node()])

    types = {node.type for node in nodes}
    assert "github_repo" in types
    assert "github_workflow_file" in types
    assert "github_workflow" in types
    publishes_edges = [
        edge
        for edge in edges
        if edge.relationship_type == "publishes_image_to"
        and edge.to_resource_id == "arn:aws:ecr:us-east-1:123456789012:repository/api"
    ]
    assert publishes_edges, "expected workflow/file references to link to the ECR repository"


@pytest.mark.skipif(importlib.util.find_spec("hcl2") is None, reason="python-hcl2 not available")
def test_github_collector_extracts_terraform_modules():
    files = {
        "infra/service/main.tf": """
module "api_service" {
  source  = "./modules/service"
  version = "1.0.0"
}
""",
    }
    repo = _FakeRepo("beamreach/infra", files)
    github = _FakeGithub([repo])
    collector = GitHubGraphCollector(
        org_id="org-1",
        github_config={"token": "dummy-token", "orgs": ["beamreach"]},
        github_client=github,
    )

    nodes, edges = collector.collect(existing_nodes=[])

    module_nodes = [node for node in nodes if node.type == "terraform_module"]
    assert module_nodes
    defines_edges = [edge for edge in edges if edge.relationship_type == "defines_module"]
    assert defines_edges


@pytest.mark.skipif(importlib.util.find_spec("hcl2") is None, reason="python-hcl2 not available")
def test_github_collector_emits_root_module_and_scoped_tf_address():
    files = {
        "infra/environments/demo/main.tf": """
terraform {
  backend "s3" {
    bucket = "beamreach-tf-states"
    key    = "coworkers/demo/terraform.tfstate"
    region = "us-east-1"
  }
}

module "service" {
  source = "../../modules/service"
}
""",
        "infra/modules/service/main.tf": """
resource "aws_lambda_function" "handler" {}
""",
    }
    repo = _FakeRepo("beamreach/platform", files)
    github = _FakeGithub([repo])
    collector = GitHubGraphCollector(
        org_id="org-1",
        github_config={"token": "dummy-token", "orgs": ["beamreach"]},
        github_client=github,
    )

    nodes, edges = collector.collect(existing_nodes=[])

    node_types = {node.type for node in nodes}
    assert "tf_root_module" in node_types
    assert "tf_address" in node_types
    writes_edges = [edge for edge in edges if edge.relationship_type == "writes"]
    assert writes_edges
    declares_edges = [edge for edge in edges if edge.relationship_type == "declares"]
    assert declares_edges
    assert any("module.service.aws_lambda_function.handler" in node.name for node in nodes if node.type == "tf_address")


@pytest.mark.skipif(importlib.util.find_spec("hcl2") is None, reason="python-hcl2 not available")
def test_github_collector_emits_fact_batches_for_terraform_topology():
    files = {
        "infra/environments/demo/main.tf": """
terraform {
  backend "s3" {
    bucket = "beamreach-tf-states"
    key    = "coworkers/demo/terraform.tfstate"
  }
}

module "service" {
  source = "../../modules/service"
}
""",
        "infra/modules/service/main.tf": """
resource "aws_lambda_function" "handler" {}
""",
    }
    repo = _FakeRepo("beamreach/platform", files)
    github = _FakeGithub([repo])
    collector = GitHubGraphCollector(
        org_id="org-1",
        github_config={"token": "dummy-token", "orgs": ["beamreach"]},
        github_client=github,
    )

    batches = collector.collect_fact_batches(existing_nodes=[])

    assert batches
    node_types = {fact.node_type for batch in batches for fact in batch.node_facts}
    edge_types = {fact.edge_type for batch in batches for fact in batch.edge_facts}
    assert "tf_root_module" in node_types
    assert "tf_address" in node_types
    assert "writes" in edge_types
    assert "part_of" in edge_types
    assert "declares" in edge_types


def test_without_an_injected_client_the_collector_builds_one_against_github_api_url(monkeypatch):
    """The production path: no client passed in, a token in the config. (A constructor
    parameter once shadowed the helper and every real scan failed at the GitHub step.)"""
    import compass_core.github as core_github
    from captains.mapper import github_mapper

    monkeypatch.setenv("GITHUB_API_URL", "http://github.example.com/api/v3/")
    built = []
    monkeypatch.setattr(github_mapper, "client_for_token", lambda token: built.append((token, core_github.api_url())) or object())
    github_mapper.GitHubGraphCollector("org", {"token": "t0ken"})
    assert built == [("t0ken", "http://github.example.com/api/v3")]
