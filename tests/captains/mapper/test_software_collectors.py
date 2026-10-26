"""The collectors put software and exposure on the map: images per task definition resolved
through ECR, a Lambda's runtime, layers and function URL, an instance's AMI, public address and
packages from SSM, a security group's rules open to the world, and every software file in a
repository (docs/specs/patching-captain.md §3.3)."""

from __future__ import annotations

import json

import pytest

from captains.mapper.aws_mapper import AWSGraphCollector
from captains.mapper.github_mapper import GitHubGraphCollector
from tests.captains.mapper.test_github_mapper import _FakeGithub, _FakeRepo

ACCOUNT = "123456789012"
REGION = "us-east-1"
DIGEST = "sha256:" + "d" * 64


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_kw):
        return self._pages


class _FakeEcs:
    def describe_task_definition(self, taskDefinition, include=None):
        return {"taskDefinition": {"family": "web", "revision": 41, "containerDefinitions": [
            {"name": "web", "image": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/acme-web:41"},
            {"name": "proxy", "image": "nginx:1.27.1"}]}, "tags": []}


class _FakeEcr:
    calls = []

    def describe_images(self, repositoryName, imageIds):
        _FakeEcr.calls.append((repositoryName, imageIds))
        return {"imageDetails": [{"imageDigest": DIGEST, "imageTags": [imageIds[0]["imageTag"]]}]}


class _FakeLambda:
    def get_paginator(self, name):
        assert name == "list_functions"
        return _Paginator([{"Functions": [
            {"FunctionName": "web", "FunctionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:web", "Runtime": "provided.al2", "Handler": "public/index.php",
             "Layers": [{"Arn": f"arn:aws:lambda:{REGION}:534081306603:layer:php-83-fpm:51", "CodeSize": 1}]},
            {"FunctionName": "worker", "FunctionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:worker", "Runtime": "python3.10", "Handler": "app.handler"},
        ]}])

    def list_function_url_configs(self, FunctionName):
        if FunctionName == "web":
            return {"FunctionUrlConfigs": [{"FunctionUrl": "https://abc.lambda-url.us-east-1.on.aws/", "AuthType": "NONE"}]}
        return {"FunctionUrlConfigs": []}


class _FakeEc2:
    def get_paginator(self, name):
        if name == "describe_security_groups":
            return _Paginator([{"SecurityGroups": [
                {"GroupId": "sg-open", "GroupName": "web-sg", "VpcId": "vpc-1", "IpPermissions": [{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]},
                {"GroupId": "sg-office", "GroupName": "office-sg", "VpcId": "vpc-1", "IpPermissions": [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "10.0.0.0/8"}]}]},
            ]}])
        if name == "describe_network_interfaces":
            return _Paginator([{"NetworkInterfaces": []}])
        if name == "describe_instances":
            return _Paginator([{"Reservations": [{"Instances": [
                {"InstanceId": "i-public", "InstanceType": "t3.small", "State": {"Name": "running"}, "Placement": {"AvailabilityZone": "us-east-1a"},
                 "ImageId": "ami-0abc", "PublicIpAddress": "54.1.2.3", "PublicDnsName": "ec2-54-1-2-3.compute.amazonaws.com", "PlatformDetails": "Linux/UNIX",
                 "SecurityGroups": [{"GroupId": "sg-open"}], "Tags": [{"Key": "Name", "Value": "legacy-php"}]},
                {"InstanceId": "i-private", "InstanceType": "t3.small", "State": {"Name": "running"}, "Placement": {"AvailabilityZone": "us-east-1a"},
                 "ImageId": "ami-0def", "SecurityGroups": [{"GroupId": "sg-office"}]},
            ]}]}])
        raise AssertionError(name)


class _FakeSsm:
    def list_inventory_entries(self, InstanceId, TypeName, NextToken=None):
        if TypeName == "AWS:InstanceInformation":
            return {"Entries": [{"PlatformName": "Ubuntu", "PlatformVersion": "24.04"}]}
        if InstanceId == "i-public" and NextToken is None:
            return {"Entries": [{"Name": "php8.3-fpm", "Version": "8.3.6-0ubuntu0.24.04.12", "Architecture": "amd64"}], "NextToken": "p2"}
        if InstanceId == "i-public":
            return {"Entries": [{"Name": "nginx", "Version": "1.24.0-2ubuntu7", "Architecture": "amd64"}]}
        return {"Entries": []}


class _FakeSession:
    def __init__(self, clients):
        self._clients = clients

    def client(self, name, **_kw):
        return self._clients[name]


@pytest.fixture
def collector(monkeypatch):
    _FakeEcr.calls = []
    session = _FakeSession({"ecr": _FakeEcr(), "lambda": _FakeLambda(), "ec2": _FakeEc2(), "ssm": _FakeSsm()})
    c = AWSGraphCollector(org_id="org", account_id=ACCOUNT, target_session=session, regions=[REGION])
    monkeypatch.setattr(c, "_regional_session", lambda region: session)
    return c, session


def test_a_task_definition_records_its_images_resolved_through_ecr(collector):
    c, _ = collector
    arn = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/web:41"
    c._ensure_task_definition(_FakeEcs(), arn, REGION)
    c._ensure_task_definition(_FakeEcs(), arn.replace(":41", ":42"), REGION)
    node = c.nodes[arn]
    assert node.attributes["images"] == [{"container": "web", "image": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/acme-web:41", "digest": DIGEST},
                                         {"container": "proxy", "image": "nginx:1.27.1"}]
    assert [s["purl"] for s in node.attributes["software"]] == [
        f"pkg:docker/acme-web@41?container=web&digest={DIGEST}&repository_url={ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/acme-web",
        "pkg:docker/nginx@1.27.1?container=proxy"]
    assert _FakeEcr.calls == [("acme-web", [{"imageTag": "41"}])], "one ECR call per tag, cached across revisions"


def test_lambda_functions_carry_runtime_layers_and_their_url(collector):
    c, session = collector
    c._collect_lambda(session, REGION)
    web = c.nodes[f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:web"]
    assert web.attributes["layers"] == [f"arn:aws:lambda:{REGION}:534081306603:layer:php-83-fpm:51"] and web.attributes["packageType"] == "Zip"
    assert web.attributes["functionUrl"] == "https://abc.lambda-url.us-east-1.on.aws/"
    assert [s["purl"] for s in web.attributes["software"]] == ["pkg:generic/aws-lambda-runtime@provided.al2",
                                                               f"pkg:generic/bref-layer/php-83-fpm@51?arn=arn:aws:lambda:{REGION}:534081306603:layer:php-83-fpm:51"]
    worker = c.nodes[f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:worker"]
    assert "functionUrl" not in worker.attributes and [s["purl"] for s in worker.attributes["software"]] == ["pkg:generic/aws-lambda-runtime@python3.10"]


def test_instances_carry_their_ami_public_address_ssm_packages_and_groups_their_open_rules(collector):
    c, session = collector
    c._collect_ec2(session, REGION)
    public = c.nodes["i-public"]
    assert public.attributes["imageId"] == "ami-0abc" and public.attributes["publicIp"] == "54.1.2.3" and public.attributes["platformName"] == "Ubuntu"
    assert [s["purl"] for s in public.attributes["software"]] == ["pkg:generic/ami@ami-0abc",
                                                                  "pkg:deb/ubuntu/php8.3-fpm@8.3.6-0ubuntu0.24.04.12?arch=amd64", "pkg:deb/ubuntu/nginx@1.24.0-2ubuntu7?arch=amd64"]
    assert public.attributes["software_count"] == 3
    private = c.nodes["i-private"]
    assert private.attributes["publicIp"] is None and [s["purl"] for s in private.attributes["software"]] == ["pkg:generic/ami@ami-0def"]
    assert c.nodes["sg-open"].attributes["publicIngress"] == [{"protocol": "tcp", "from_port": 443, "to_port": 443, "cidrs": ["0.0.0.0/0"]}]
    assert "publicIngress" not in c.nodes["sg-office"].attributes


def test_the_exposure_pass_runs_on_what_the_collectors_produced(collector):
    from captains.mapper.software import annotate_exposure

    c, session = collector
    c._collect_ec2(session, REGION)
    c._collect_lambda(session, REGION)
    nodes, edges = list(c.nodes.values()), list(c.edges.values())
    annotate_exposure(nodes, edges)
    by_id = {n.resource_id: n.attributes for n in nodes}
    assert by_id["i-public"]["exposed"] is True and by_id["i-private"]["exposed"] is False
    assert by_id[f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:web"]["exposed"] is True
    assert by_id[f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:worker"]["exposed"] is False


# ── the repositories ──────────────────────────────────────────────────────────

REPO_FILES = {
    "web/Dockerfile": "FROM php:8.3.30-fpm\nRUN apt-get update && apt-get install -y libzip-dev\n",
    "web/composer.json": json.dumps({"require": {"laravel/framework": "^11.2"}}),
    "web/composer.lock": json.dumps({"packages": [{"name": "laravel/framework", "version": "v11.2.0"}, {"name": "symfony/http-foundation", "version": "v7.1.3"}], "packages-dev": []}),
    "functions/serverless.yml": "service: fns\nprovider:\n  runtime: provided.al2\nfunctions:\n  web:\n    handler: index.php\n    layers:\n      - arn:aws:lambda:us-east-1:534081306603:layer:php-83-fpm:51\n",
    "infra/main.tf": 'resource "aws_instance" "legacy" {\n  ami = "ami-0abcdef1234567890"\n  instance_type = "t3.small"\n}\n',
    "README.md": "# web\n",
}


@pytest.mark.skipif(pytest.importorskip("hcl2", reason="python-hcl2 not available") is None, reason="python-hcl2 not available")
def test_the_github_collector_reads_every_software_file_and_ties_the_lockfile_to_its_manifest():
    collector = GitHubGraphCollector(org_id="org-1", github_config={"token": "dummy", "orgs": ["acme"]}, github_client=_FakeGithub([_FakeRepo("acme/web", REPO_FILES)]))
    nodes, _ = collector.collect(existing_nodes=[])
    by_path = {n.attributes.get("path"): n for n in nodes if n.attributes.get("path")}
    assert by_path["web/composer.json"].type == "github_manifest_file" and by_path["web/composer.lock"].type == "github_lockfile"
    assert by_path["functions/serverless.yml"].type == "github_deploy_file" and "README.md" not in by_path
    dockerfile = by_path["web/Dockerfile"].attributes
    assert dockerfile["base_images"] == ["php:8.3.30-fpm"]
    assert [s["purl"] for s in dockerfile["software"]] == ["pkg:docker/php@8.3.30-fpm?final=true&stage=0", "pkg:deb/debian/libzip-dev?distro=debian&final=true&stage=0"]
    assert [s["purl"] for s in by_path["web/composer.json"].attributes["software"]] == ["pkg:composer/laravel/framework?constraint=%5E11.2"]
    lock = {s["name"]: s["relation"] for s in by_path["web/composer.lock"].attributes["software"]}
    assert lock == {"laravel/framework": "direct", "symfony/http-foundation": "transitive"}
    assert [s["purl"] for s in by_path["functions/serverless.yml"].attributes["software"]] == [
        "pkg:generic/aws-lambda-runtime@provided.al2", "pkg:generic/bref-layer/php-83-fpm@51?arn=arn:aws:lambda:us-east-1:534081306603:layer:php-83-fpm:51"]
    assert [s["purl"] for s in by_path["infra/main.tf"].attributes["software"]] == ["pkg:generic/ami@ami-0abcdef1234567890"]
