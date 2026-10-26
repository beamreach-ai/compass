"""Software on the map: purls from Dockerfiles, manifests, lockfiles, deploy descriptors, task
definitions, Lambda configurations and instances; exposure from the map's edges
(docs/specs/patching-captain.md §3.1, §3.3, §6 "Inventory")."""

from __future__ import annotations

import json

import pytest

from captains.mapper import software as sw
from infra_map.models import RelationshipEdge, ResourceNode


def purls(entries, **where):
    return [e.purl for e in entries if all(getattr(e, k, None) == v or e.qualifiers.get(k) == v for k, v in where.items())]


# ── images and purls ──────────────────────────────────────────────────────────

def test_image_references_become_docker_purls_with_the_registry_kept_as_a_qualifier():
    assert sw.image_software("php:8.3.30-fpm", "Dockerfile:1").purl == "pkg:docker/php@8.3.30-fpm"
    assert sw.image_software("docker.io/library/node:20-alpine", "x").purl == "pkg:docker/node@20-alpine"
    assert sw.image_software("public.ecr.aws/docker/library/python:3.11", "x").purl == "pkg:docker/python@3.11"
    ecr = sw.image_software("123456789012.dkr.ecr.us-east-1.amazonaws.com/acme-web:41", "x", digest="sha256:" + "a" * 64)
    assert ecr.purl == "pkg:docker/acme-web@41?digest=sha256:" + "a" * 64 + "&repository_url=123456789012.dkr.ecr.us-east-1.amazonaws.com/acme-web"
    assert ecr.name == "acme-web" and ecr.version == "41"
    pinned = sw.image_software("ghcr.io/acme/web@sha256:" + "b" * 64, "x")
    assert pinned.version is None and pinned.qualifiers["digest"].startswith("sha256:b")
    assert sw.ecr_parts("123456789012.dkr.ecr.us-east-1.amazonaws.com/acme/web:41") == ("123456789012", "us-east-1", "acme/web", "41")
    assert sw.ecr_parts("php:8.3") is None
    assert sw.parse_image_ref("${BASE_IMAGE}") is None
    assert sw.purl("npm", "lodash", "4.17.21") == "pkg:npm/lodash@4.17.21"
    assert sw.purl("composer", "framework", "11.2.0", namespace="laravel") == "pkg:composer/laravel/framework@11.2.0"
    assert sw.purl("deb", "php8.3-fpm", "8.3.6-0ubuntu0.24.04.12", namespace="ubuntu", qualifiers={"distro": "ubuntu-24.04"}) == \
        "pkg:deb/ubuntu/php8.3-fpm@8.3.6-0ubuntu0.24.04.12?distro=ubuntu-24.04"


def test_the_distro_inside_a_base_image_is_read_from_its_name_and_tag():
    def distro(ref):
        return sw.base_distro(sw.parse_image_ref(ref))
    assert distro("ubuntu:24.04") == ("deb", "ubuntu", "24.04")
    assert distro("ubuntu:noble") == ("deb", "ubuntu", "24.04")
    assert distro("debian:12-slim") == ("deb", "debian", "bookworm")
    assert distro("php:8.3-fpm") == ("deb", "debian", None)
    assert distro("php:8.3-fpm-bookworm") == ("deb", "debian", "bookworm")
    assert distro("php:8.3-fpm-alpine3.20") == ("apk", "alpine", "3.20")
    assert distro("alpine:3.20") == ("apk", "alpine", "3.20")
    assert distro("amazonlinux:2023") == ("rpm", "amazonlinux", "2023")
    assert distro("123456789012.dkr.ecr.us-east-1.amazonaws.com/acme-base:1") is None


# ── Dockerfiles ───────────────────────────────────────────────────────────────

PHP_SHOP = """
# syntax=docker/dockerfile:1
ARG PHP_VERSION=8.3.30
FROM composer:2 AS vendor
WORKDIR /app
COPY composer.json composer.lock ./
RUN composer install --no-dev

FROM php:${PHP_VERSION}-fpm@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
RUN apt-get update && apt-get install -y --no-install-recommends \\
      libzip-dev=1.7.3-1+b1 \\
      git \\
    && docker-php-ext-install zip \\
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install awscli==1.33.0 && composer global require laravel/envoy:^2.8
COPY --from=vendor /app/vendor /var/www/vendor
"""


def test_a_multi_stage_dockerfile_yields_every_stage_and_marks_what_the_final_image_runs():
    parsed = sw.parse_dockerfile(PHP_SHOP, path="web/Dockerfile")
    assert parsed.base_images == ["composer:2", "php:8.3.30-fpm@sha256:" + "0123456789abcdef" * 4]
    assert [s.name for s in parsed.stages] == ["vendor", None]
    base = [e for e in parsed.software if e.relation == "base" and e.name == "php"][0]
    assert base.purl.startswith("pkg:docker/php@8.3.30-fpm?digest=sha256:0123456789abcdef")
    assert base.qualifiers["final"] == "true" and base.evidence == "web/Dockerfile:9"
    composer_base = [e for e in parsed.software if e.name == "composer"][0]
    assert "final" not in composer_base.qualifiers, "the vendor stage is a build stage"
    libzip = [e for e in parsed.software if e.name == "debian/libzip-dev"][0]
    assert libzip.purl == "pkg:deb/debian/libzip-dev@1.7.3-1%2Bb1?distro=debian&final=true&stage=1"
    assert libzip.evidence == "web/Dockerfile:10"
    git = [e for e in parsed.software if e.name == "debian/git"][0]
    assert git.version is None and git.purl == "pkg:deb/debian/git?distro=debian&final=true&stage=1"
    assert purls(parsed.software, type="pypi") == ["pkg:pypi/awscli@1.33.0?final=true&stage=1"]
    envoy = [e for e in parsed.software if e.type == "composer"][0]
    assert envoy.relation == "tool" and envoy.name == "laravel/envoy" and envoy.qualifiers["constraint"] == "^2.8"
    assert parsed.notes == []


def test_ubuntu_apt_lines_alpine_apk_lines_and_a_stage_that_builds_on_another():
    ubuntu = sw.parse_dockerfile("FROM ubuntu:24.04\nRUN DEBIAN_FRONTEND=noninteractive apt install -y php8.3-fpm=8.3.6-0ubuntu0.24.04.12 nginx; apt-get clean\n")
    assert purls(ubuntu.software, type="deb") == ["pkg:deb/ubuntu/php8.3-fpm@8.3.6-0ubuntu0.24.04.12?distro=ubuntu-24.04&final=true&stage=0",
                                                   "pkg:deb/ubuntu/nginx?distro=ubuntu-24.04&final=true&stage=0"]
    alpine = sw.parse_dockerfile("FROM alpine:3.20 AS base\nRUN apk add --no-cache php83=8.3.12-r0 php83-fpm\nFROM base\nRUN apk add curl\n")
    assert [s.parent for s in alpine.stages] == [None, 0]
    assert purls(alpine.software, type="apk", final="true") == ["pkg:apk/alpine/php83@8.3.12-r0?distro=alpine-3.20&final=true&stage=0",
                                                                "pkg:apk/alpine/php83-fpm?distro=alpine-3.20&final=true&stage=0",
                                                                "pkg:apk/alpine/curl?distro=alpine-3.20&final=true&stage=1"]
    unresolved = sw.parse_dockerfile("ARG BASE\nFROM ${BASE}\nRUN apt-get install -y curl\n")
    assert unresolved.base_images == [] and unresolved.notes and "build argument" in unresolved.notes[0]
    assert purls(unresolved.software) == ["pkg:deb/debian/curl?distro=debian&final=true&stage=0"], "the packages are still read, on an unknown base"
    exec_form = sw.parse_dockerfile('FROM amazonlinux:2023\nRUN ["dnf", "install", "-y", "php8.3-fpm"]\n')
    assert purls(exec_form.software, type="rpm") == ["pkg:rpm/amazonlinux/php8.3-fpm?distro=amazonlinux-2023&final=true&stage=0"]
    node = sw.parse_dockerfile("FROM node:20-bookworm-slim\nRUN npm install -g pnpm@9.1.0 && npm ci\n")
    assert purls(node.software, type="npm") == ["pkg:npm/pnpm@9.1.0?final=true&stage=0"]


# ── manifests and lockfiles ───────────────────────────────────────────────────

COMPOSER_JSON = json.dumps({"require": {"php": "^8.3", "laravel/framework": "11.2.0", "bref/bref": "^2.3"}, "require-dev": {"phpunit/phpunit": "^11"}})
COMPOSER_LOCK = json.dumps({"packages": [{"name": "laravel/framework", "version": "v11.2.0"}, {"name": "symfony/http-foundation", "version": "v7.1.3"},
                                         {"name": "bref/bref", "version": "2.3.14"}],
                            "packages-dev": [{"name": "phpunit/phpunit", "version": "11.3.0"}]})


def test_composer_manifest_and_lock_tell_direct_from_transitive_and_dev():
    manifest = sw.parse_manifest("api/composer.json", COMPOSER_JSON)
    assert purls(manifest, relation="direct") == ["pkg:composer/laravel/framework@11.2.0", "pkg:composer/bref/bref?constraint=%5E2.3"]
    assert purls(manifest, relation="dev") == ["pkg:composer/phpunit/phpunit?constraint=%5E11"]
    lock = sw.parse_lockfile("api/composer.lock", COMPOSER_LOCK, sw.direct_names(manifest))
    assert {(e.name, e.version, e.relation) for e in lock} == {("laravel/framework", "11.2.0", "direct"), ("symfony/http-foundation", "7.1.3", "transitive"),
                                                               ("bref/bref", "2.3.14", "direct"), ("phpunit/phpunit", "11.3.0", "dev")}
    files = {"api/composer.json": ("manifest", COMPOSER_JSON), "api/composer.lock": ("lockfile", COMPOSER_LOCK)}
    together = sw.repository_software(files)
    assert [e.relation for e in together["api/composer.lock"] if e.name == "symfony/http-foundation"] == ["transitive"]


def test_npm_lockfiles_in_their_three_dialects():
    package_json = json.dumps({"dependencies": {"express": "^4.19.2", "@aws-sdk/client-s3": "3.600.0"}, "devDependencies": {"vitest": "^2"}})
    manifest = sw.parse_manifest("package.json", package_json)
    assert purls(manifest) == ["pkg:npm/express", "pkg:npm/%40aws-sdk/client-s3@3.600.0", "pkg:npm/vitest"]
    v3 = json.dumps({"lockfileVersion": 3, "packages": {"": {"dependencies": {"express": "^4.19.2"}, "devDependencies": {"vitest": "^2"}},
                                                        "node_modules/express": {"version": "4.19.2"}, "node_modules/qs": {"version": "6.11.0"},
                                                        "node_modules/vitest": {"version": "2.0.5", "dev": True},
                                                        "node_modules/@aws-sdk/client-s3": {"version": "3.600.0"}}})
    lock = sw.parse_lockfile("package-lock.json", v3, sw.direct_names(manifest))
    assert {(e.name, e.version, e.relation) for e in lock} == {("express", "4.19.2", "direct"), ("qs", "6.11.0", "transitive"), ("vitest", "2.0.5", "dev"),
                                                               ("@aws-sdk/client-s3", "3.600.0", "direct")}
    assert [e.purl for e in lock if e.name.startswith("@")] == ["pkg:npm/%40aws-sdk/client-s3@3.600.0"]
    yarn = '# yarn lockfile v1\n\n"@babel/core@^7.0.0", "@babel/core@^7.2.0":\n  version "7.24.0"\n  resolved "x"\n\nexpress@^4.19.2:\n  version "4.19.2"\n'
    assert {(e.name, e.version) for e in sw.parse_lockfile("yarn.lock", yarn)} == {("@babel/core", "7.24.0"), ("express", "4.19.2")}
    berry = '"lodash@npm:^4.17.21":\n  version: 4.17.21\n  resolution: "lodash@npm:4.17.21"\n'
    assert purls(sw.parse_lockfile("yarn.lock", berry)) == ["pkg:npm/lodash@4.17.21"]
    pnpm = "lockfileVersion: '9.0'\nimporters:\n  .:\n    dependencies:\n      express:\n        specifier: ^4.19.2\n        version: 4.19.2\npackages:\n  express@4.19.2:\n    resolution: {integrity: x}\n  qs@6.11.0:\n    resolution: {integrity: y}\n  '@types/node@20.1.0':\n    resolution: {integrity: z}\n"
    rows = sw.parse_lockfile("pnpm-lock.yaml", pnpm)
    assert {(e.name, e.version, e.relation) for e in rows} == {("express", "4.19.2", "direct"), ("qs", "6.11.0", "transitive"), ("@types/node", "20.1.0", "transitive")}


def test_python_go_and_rust_files():
    reqs = "requests==2.31.0  # http\nboto3>=1.34\n-r base.txt\nDjango[argon2]==5.0.6 ; python_version > '3.9'\n"
    assert purls(sw.parse_manifest("requirements.txt", reqs)) == ["pkg:pypi/requests@2.31.0", "pkg:pypi/boto3", "pkg:pypi/django@5.0.6"]
    pyproject = '[project]\nname = "x"\ndependencies = ["fastapi>=0.115", "PyJWT==2.9.0"]\n[project.optional-dependencies]\ndev = ["pytest"]\n[tool.poetry.dependencies]\npython = "^3.12"\nhttpx = {version = "0.27.0", extras = ["http2"]}\n'
    entries = sw.parse_manifest("pyproject.toml", pyproject)
    assert {(e.name, e.version, e.relation) for e in entries} == {("fastapi", None, "direct"), ("pyjwt", "2.9.0", "direct"), ("pytest", None, "dev"), ("httpx", "0.27.0", "direct")}
    uv = '[[package]]\nname = "fastapi"\nversion = "0.115.0"\n\n[[package]]\nname = "starlette"\nversion = "0.38.6"\n\n[[package]]\nname = "x"\nversion = "0.1.0"\nsource = { editable = "." }\n'
    assert {(e.name, e.relation) for e in sw.parse_lockfile("uv.lock", uv, {"fastapi"})} == {("fastapi", "direct"), ("starlette", "transitive")}
    poetry = '[[package]]\nname = "pytest"\nversion = "8.3.2"\ncategory = "dev"\n'
    assert [e.relation for e in sw.parse_lockfile("poetry.lock", poetry)] == ["dev"]
    gomod = "module acme/api\n\ngo 1.22\n\nrequire (\n\tgithub.com/aws/aws-sdk-go-v2 v1.30.3\n\tgolang.org/x/net v0.27.0 // indirect\n)\nrequire github.com/gin-gonic/gin v1.10.0\n"
    assert {(e.name, e.version, e.relation) for e in sw.parse_manifest("go.mod", gomod)} == {("github.com/aws/aws-sdk-go-v2", "v1.30.3", "direct"),
                                                                                             ("golang.org/x/net", "v0.27.0", "dev"), ("github.com/gin-gonic/gin", "v1.10.0", "direct")}
    gosum = "github.com/gin-gonic/gin v1.10.0 h1:x\ngithub.com/gin-gonic/gin v1.10.0/go.mod h1:y\ngolang.org/x/net v0.27.0 h1:z\n"
    assert purls(sw.parse_lockfile("go.sum", gosum, {"github.com/gin-gonic/gin"})) == ["pkg:golang/github.com/gin-gonic/gin@v1.10.0", "pkg:golang/golang.org/x/net@v0.27.0"]
    cargo = '[package]\nname = "svc"\n[dependencies]\nserde = { version = "1.0.203", features = ["derive"] }\ntokio = "1"\n[dev-dependencies]\nproptest = "1.5.0"\n'
    assert {(e.name, e.version, e.relation) for e in sw.parse_manifest("Cargo.toml", cargo)} == {("serde", "1.0.203", "direct"), ("tokio", None, "direct"), ("proptest", "1.5.0", "dev")}
    cargo_lock = '[[package]]\nname = "serde"\nversion = "1.0.203"\n\n[[package]]\nname = "syn"\nversion = "2.0.68"\n'
    assert purls(sw.parse_lockfile("Cargo.lock", cargo_lock, {"serde"}), relation="transitive") == ["pkg:cargo/syn@2.0.68"]
    assert sw.parse_manifest("composer.json", "{not json") == [] and sw.parse_lockfile("package-lock.json", "{") == []


def test_file_classification_covers_the_names_the_mapper_reads():
    assert sw.classify_software_file("api/composer.json") == "manifest"
    assert sw.classify_software_file("requirements-dev.txt") == "manifest"
    assert sw.classify_software_file("web/pnpm-lock.yaml") == "lockfile"
    assert sw.classify_software_file("functions/serverless.yml") == "deploy"
    assert sw.classify_software_file("template.yaml") == "deploy"
    assert sw.classify_software_file("README.md") is None


# ── deploy descriptors ────────────────────────────────────────────────────────

SERVERLESS = """
service: acme-functions
provider:
  name: aws
  runtime: provided.al2
  layers:
    - ${bref:layer.php-83-fpm}
functions:
  web:
    handler: public/index.php
    layers:
      - arn:aws:lambda:us-east-1:534081306603:layer:php-83-fpm:51
  worker:
    handler: worker.php
    runtime: python3.10
    layers:
      - arn:aws:lambda:us-east-1:123456789012:layer:shared-libs:7
  image-fn:
    image:
      uri: 123456789012.dkr.ecr.us-east-1.amazonaws.com/acme-fn:3
"""


def test_serverless_and_sam_descriptors_yield_runtimes_layers_and_images():
    entries = sw.parse_deploy("functions/serverless.yml", SERVERLESS)
    assert purls(entries, relation="runtime") == ["pkg:generic/aws-lambda-runtime@provided.al2", "pkg:generic/aws-lambda-runtime@python3.10"]
    bref = [e for e in entries if e.namespace == "bref-layer"]
    assert [e.purl for e in bref] == ["pkg:generic/bref-layer/php-83-fpm@51?arn=arn:aws:lambda:us-east-1:534081306603:layer:php-83-fpm:51"]
    assert [e.purl for e in entries if e.namespace == "lambda-layer"] == ["pkg:generic/lambda-layer/shared-libs@7?arn=arn:aws:lambda:us-east-1:123456789012:layer:shared-libs:7"]
    assert purls(entries, relation="image")[0].startswith("pkg:docker/acme-fn@3?")
    shorthand = sw.parse_deploy("serverless.yml", "provider:\n  runtime: provided.al2\nfunctions:\n  api:\n    handler: index.php\n    layers:\n      - ${bref:layer.php-83}\n")
    assert [e.purl for e in shorthand if e.relation == "layer"] == ["pkg:generic/bref-layer/php-83?resolved_by=serverless-bref-plugin"]
    sam = "Globals:\n  Function:\n    Runtime: python3.10\nResources:\n  Api:\n    Type: AWS::Serverless::Function\n    Properties:\n      Handler: app.handler\n      Layers:\n        - !Sub arn:aws:lambda:us-east-1:534081306603:layer:php-83:52\n  Bucket:\n    Type: AWS::S3::Bucket\n"
    sam_entries = sw.parse_deploy("template.yaml", sam)
    assert purls(sam_entries) == ["pkg:generic/aws-lambda-runtime@python3.10", "pkg:generic/bref-layer/php-83@52?arn=arn:aws:lambda:us-east-1:534081306603:layer:php-83:52"]


@pytest.mark.skipif(pytest.importorskip("hcl2", reason="python-hcl2 not available") is None, reason="python-hcl2 not available")
def test_terraform_lambda_runtimes_layers_amis_and_ami_filters():
    import hcl2

    from captains.mapper.github_mapper import _normalize_hcl_keys

    text = '''
resource "aws_lambda_function" "api" {
  function_name = "api"
  runtime       = "python3.10"
  layers        = ["arn:aws:lambda:us-east-1:534081306603:layer:php-83-fpm:51"]
}
resource "aws_lambda_function" "img" {
  function_name = "img"
  image_uri     = "123456789012.dkr.ecr.us-east-1.amazonaws.com/acme-fn:3"
}
resource "aws_instance" "web" {
  ami           = "ami-0abcdef1234567890"
  instance_type = "t3.small"
}
resource "aws_instance" "dyn" {
  ami = var.ami
}
resource "aws_launch_template" "lt" {
  image_id = "ami-0fedcba9876543210"
}
data "aws_ami" "ubuntu" {
  most_recent = true
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-noble-24.04-amd64-server-*"]
  }
}
'''
    entries = sw.terraform_software(_normalize_hcl_keys(hcl2.loads(text)), "infra/main.tf")
    by_evidence = {e.evidence: e.purl for e in entries}
    assert by_evidence["infra/main.tf:aws_lambda_function.api"] in ("pkg:generic/aws-lambda-runtime@python3.10",
                                                                     "pkg:generic/bref-layer/php-83-fpm@51?arn=arn:aws:lambda:us-east-1:534081306603:layer:php-83-fpm:51")
    assert sorted(e.purl for e in entries if e.evidence.endswith("function.api")) == ["pkg:generic/aws-lambda-runtime@python3.10",
                                                                                      "pkg:generic/bref-layer/php-83-fpm@51?arn=arn:aws:lambda:us-east-1:534081306603:layer:php-83-fpm:51"]
    assert by_evidence["infra/main.tf:aws_lambda_function.img"].startswith("pkg:docker/acme-fn@3?")
    assert by_evidence["infra/main.tf:aws_instance.web"] == "pkg:generic/ami@ami-0abcdef1234567890"
    assert "infra/main.tf:aws_instance.dyn" not in by_evidence, "an expression is not a version"
    assert by_evidence["infra/main.tf:aws_launch_template.lt"] == "pkg:generic/ami@ami-0fedcba9876543210"
    assert by_evidence["infra/main.tf:data.aws_ami.ubuntu"] == "pkg:generic/ami?filter=ubuntu/images/hvm-ssd/ubuntu-noble-24.04-amd64-server-%2A&most_recent=true"


# ── the AWS side ──────────────────────────────────────────────────────────────

def test_task_definitions_resolve_their_ecr_images_to_digests_and_keep_the_rest_as_tags():
    task_def = {"family": "web", "containerDefinitions": [
        {"name": "web", "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/acme-web:41"},
        {"name": "proxy", "image": "nginx:1.27.1"},
        {"name": "agent", "image": "public.ecr.aws/datadog/agent:7"},
    ]}
    calls = []

    def resolve(account, region, repository, tag):
        calls.append((account, region, repository, tag))
        return "sha256:" + "c" * 64

    images, entries = sw.task_definition_software(task_def, resolve)
    assert calls == [("123456789012", "us-east-1", "acme-web", "41")]
    assert images[0] == {"container": "web", "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/acme-web:41", "digest": "sha256:" + "c" * 64}
    assert images[1] == {"container": "proxy", "image": "nginx:1.27.1"}
    assert [e.purl for e in entries] == ["pkg:docker/acme-web@41?container=web&digest=sha256:" + "c" * 64 + "&repository_url=123456789012.dkr.ecr.us-east-1.amazonaws.com/acme-web",
                                         "pkg:docker/nginx@1.27.1?container=proxy",
                                         "pkg:docker/datadog/agent@7?container=agent&repository_url=public.ecr.aws/datadog/agent"]

    def broken(*_):
        raise RuntimeError("no ecr")

    images, _ = sw.task_definition_software(task_def, broken)
    assert "digest" not in images[0]


def test_lambda_functions_instances_and_ssm_inventory():
    fn = {"FunctionName": "web", "Runtime": "provided.al2", "Layers": [{"Arn": "arn:aws:lambda:us-east-1:534081306603:layer:php-83-fpm:51", "CodeSize": 1},
                                                                        {"Arn": "arn:aws:lambda:us-east-1:123456789012:layer:libs:3"}]}
    assert purls(sw.lambda_software(fn)) == ["pkg:generic/aws-lambda-runtime@provided.al2",
                                             "pkg:generic/bref-layer/php-83-fpm@51?arn=arn:aws:lambda:us-east-1:534081306603:layer:php-83-fpm:51",
                                             "pkg:generic/lambda-layer/libs@3?arn=arn:aws:lambda:us-east-1:123456789012:layer:libs:3"]
    assert sw.lambda_software({"FunctionName": "img", "PackageType": "Image"}) == []
    assert purls(sw.instance_software({"InstanceId": "i-1", "ImageId": "ami-0abc"})) == ["pkg:generic/ami@ami-0abc"]
    rows = [{"Name": "php8.3-fpm", "Version": "8.3.6-0ubuntu0.24.04.12", "Architecture": "amd64"}, {"Name": "", "Version": "1"}]
    assert purls(sw.ssm_inventory_software(rows, platform="Ubuntu")) == ["pkg:deb/ubuntu/php8.3-fpm@8.3.6-0ubuntu0.24.04.12?arch=amd64"]
    assert purls(sw.ssm_inventory_software([{"Name": "php", "Version": "8.3.12"}], platform="Amazon Linux")) == ["pkg:rpm/amazonlinux/php@8.3.12"]


def test_software_is_capped_per_node_and_the_node_says_so():
    many = [sw.Software(purl=f"pkg:npm/p{i}@1", type="npm", name=f"p{i}", version="1", evidence="lock", relation="transitive") for i in range(sw.MAX_ENTRIES + 5)]
    attributes = {}
    sw.put_software(attributes, many)
    assert len(attributes["software"]) == sw.MAX_ENTRIES and attributes["software_count"] == sw.MAX_ENTRIES + 5 and attributes["software_truncated"] is True
    assert attributes["software"][0] == {"purl": "pkg:npm/p0@1", "type": "npm", "name": "p0", "version": "1", "evidence": "lock", "relation": "transitive"}
    empty = {}
    sw.put_software(empty, [])
    assert empty == {}


# ── exposure ──────────────────────────────────────────────────────────────────

def _node(rid, ntype, name=None, **attributes):
    return ResourceNode(org_id="o", resource_id=rid, type=ntype, account_id="1", region="us-east-1", name=name or rid, attributes=attributes)


def _edge(src, dst, rel):
    return RelationshipEdge(org_id="o", from_resource_id=src, to_resource_id=dst, relationship_type=rel, confidence="high", evidence=[])


def test_exposure_comes_from_the_load_balancer_the_function_url_and_the_open_security_group():
    nodes = [
        _node("svc-web", "ecs_service", "web"), _node("tg-web", "elbv2_target_group"), _node("alb", "elbv2_load_balancer", "public-alb", scheme="internet-facing"),
        _node("td-web", "ecs_task_definition", "web:41"),
        _node("svc-internal", "ecs_service", "jobs"), _node("tg-int", "elbv2_target_group"), _node("nlb", "elbv2_load_balancer", "internal-nlb", scheme="internal"),
        _node("td-jobs", "ecs_task_definition", "jobs:3"),
        _node("fn-url", "lambda_function", functionUrl="https://abc.lambda-url.us-east-1.on.aws/"), _node("fn-quiet", "lambda_function"),
        _node("i-pub", "ec2_instance", publicIp="54.1.2.3"), _node("i-pub-closed", "ec2_instance", publicIp="54.1.2.4"), _node("i-priv", "ec2_instance"),
        _node("sg-open", "ec2_security_group", "web-sg", publicIngress=[{"protocol": "tcp", "from_port": 443, "to_port": 443, "cidrs": ["0.0.0.0/0"]}]),
        _node("sg-closed", "ec2_security_group", "office-sg"),
    ]
    edges = [_edge("svc-web", "tg-web", "exposes"), _edge("tg-web", "alb", "attached_to"), _edge("td-web", "svc-web", "uses_in"),
             _edge("svc-internal", "tg-int", "exposes"), _edge("tg-int", "nlb", "attached_to"), _edge("td-jobs", "svc-internal", "uses_in"),
             _edge("i-pub", "sg-open", "secured_by"), _edge("i-pub-closed", "sg-closed", "secured_by"), _edge("i-priv", "sg-open", "secured_by")]
    sw.annotate_exposure(nodes, edges)
    by_id = {n.resource_id: n.attributes for n in nodes}
    assert by_id["svc-web"]["exposed"] is True and by_id["svc-web"]["exposure"]["via"][0]["name"] == "public-alb"
    assert by_id["td-web"]["exposed"] is True and by_id["td-web"]["exposure"]["via"][0]["service"] == "web"
    assert by_id["svc-internal"]["exposed"] is False and by_id["td-jobs"]["exposed"] is False
    assert by_id["fn-url"]["exposed"] is True and by_id["fn-url"]["exposure"]["via"][0]["kind"] == "function_url"
    assert by_id["fn-quiet"]["exposed"] is False
    assert by_id["i-pub"]["exposed"] is True and "web-sg" in by_id["i-pub"]["exposure"]["via"][0]["evidence"]
    assert by_id["i-pub-closed"]["exposed"] is False and by_id["i-priv"]["exposed"] is False
    assert "exposed" not in by_id["alb"]
    assert sw.public_ingress({"IpPermissions": [{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}], "Ipv6Ranges": []},
                                                {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "10.0.0.0/8"}]}]}) == \
        [{"protocol": "tcp", "from_port": 443, "to_port": 443, "cidrs": ["0.0.0.0/0"]}]
