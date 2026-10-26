"""The fixes (docs/specs/patching-captain.md §3.5, §6): golden diffs per strategy, including a
digest-pinned FROM, a multi-stage Dockerfile, an apt line with other packages, a Bref layer in
serverless.yml, each ecosystem's manifest; a file that no longer holds what the finding saw."""

from __future__ import annotations

import json

from captains.patch import fixes


def finding(**over):
    base = {"advisory_id": "CVE-2026-109618", "purl": "pkg:docker/php@8.3.30-fpm?final=true&stage=1", "purl_type": "docker", "package": "php", "version": "8.3.30",
            "fixed_version": "8.3.36", "relation": "base", "path": "web/Dockerfile", "blockers": [], "key": "k1"}
    base.update(over)
    return base


DOCKERFILE = """# syntax=docker/dockerfile:1
FROM composer:2 AS vendor
COPY composer.json composer.lock ./
RUN composer install --no-dev

FROM php:8.3.30-fpm AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \\
      libzip-dev \\
      git \\
    && docker-php-ext-install zip
COPY --from=vendor /app/vendor /var/www/vendor
"""


def test_a_docker_library_tag_moves_to_the_fixed_upstream_version_and_only_that_line():
    edit = fixes.apply(finding(), DOCKERFILE)
    assert edit is not None and edit.strategy == "dockerfile_from" and edit.description == "FROM php:8.3.30-fpm → php:8.3.36-fpm"
    assert edit.after == DOCKERFILE.replace("FROM php:8.3.30-fpm AS runtime", "FROM php:8.3.36-fpm AS runtime") and "composer:2" in edit.after
    assert edit.notes == []
    alpine = finding(purl="pkg:docker/php@8.3.30-fpm-alpine3.20", version="8.3.30")
    assert fixes.apply(alpine, "FROM --platform=linux/amd64 docker.io/library/php:8.3.30-fpm-alpine3.20\n").after == "FROM --platform=linux/amd64 docker.io/library/php:8.3.36-fpm-alpine3.20\n"
    assert fixes.apply(finding(), "FROM php:8.2.9-fpm\n") is None, "the file no longer holds what the finding saw"
    assert fixes.apply(finding(fixed_version=None), DOCKERFILE) is None and fixes.apply(finding(blockers=["floating_tag"]), DOCKERFILE) is None


def test_a_digest_pinned_from_gets_the_new_digest_or_loses_the_pin_and_says_so():
    pinned = "FROM php:8.3.30-fpm@sha256:" + "0" * 64 + "\n"
    with_digest = fixes.apply(finding(), pinned, digest_for_tag=lambda image, tag: "sha256:" + "f" * 64 if tag == "8.3.36-fpm" else None)
    assert with_digest.after == "FROM php:8.3.36-fpm@sha256:" + "f" * 64 + "\n" and with_digest.notes == []
    without = fixes.apply(finding(), pinned, digest_for_tag=lambda image, tag: None)
    assert without.after == "FROM php:8.3.36-fpm\n" and without.notes == ["the digest pin on php:8.3.30-fpm could not be re-resolved for 8.3.36-fpm; the tag alone is kept"]
    assert fixes.apply(finding(), pinned).after == "FROM php:8.3.36-fpm\n", "no registry client: the pin goes too"


def test_an_apt_package_is_pinned_to_the_fixed_distro_version_among_the_other_packages():
    f = finding(purl="pkg:deb/ubuntu/php8.3-fpm?distro=ubuntu-24.04&final=true&stage=0", purl_type="deb", package="php8.3", version=None,
                fixed_version="8.3.6-0ubuntu0.24.04.13", relation="package", path="legacy/Dockerfile")
    one_line = "FROM ubuntu:24.04\nRUN apt-get update && apt-get install -y php8.3-fpm nginx curl && rm -rf /var/lib/apt/lists/*\n"
    edit = fixes.apply(f, one_line)
    assert edit.after == one_line.replace("php8.3-fpm nginx", "php8.3-fpm=8.3.6-0ubuntu0.24.04.13 nginx") and edit.description == "php8.3-fpm → php8.3-fpm=8.3.6-0ubuntu0.24.04.13"
    continued = "RUN apt-get install -y --no-install-recommends \\\n      php8.3-fpm \\\n      php8.3-cli \\\n    && apt-get clean\n"
    assert fixes.apply(f, continued).after == continued.replace("      php8.3-fpm \\", "      php8.3-fpm=8.3.6-0ubuntu0.24.04.13 \\")
    repinned = fixes.apply(dict(f, purl="pkg:deb/ubuntu/php8.3-fpm@8.3.6-0ubuntu0.24.04.12?distro=ubuntu-24.04", version="8.3.6-0ubuntu0.24.04.12"),
                           "RUN apt install -y php8.3-fpm=8.3.6-0ubuntu0.24.04.12 nginx\n")
    assert repinned.after == "RUN apt install -y php8.3-fpm=8.3.6-0ubuntu0.24.04.13 nginx\n" and repinned.description.startswith("php8.3-fpm=8.3.6-0ubuntu0.24.04.12 →")
    apk = fixes.apply(dict(f, purl="pkg:apk/alpine/php83@8.3.12-r0?distro=alpine-3.20", purl_type="apk", package="php83", version="8.3.12-r0", fixed_version="8.3.36-r0"),
                      "FROM alpine:3.20\nRUN apk add --no-cache php83=8.3.12-r0 php83-fpm\n")
    assert apk.after == "FROM alpine:3.20\nRUN apk add --no-cache php83=8.3.36-r0 php83-fpm\n"
    assert fixes.apply(f, "RUN apt-get install -y php8.1-fpm\n") is None


def test_a_bref_layer_version_moves_in_serverless_yml_and_terraform():
    f = finding(purl="pkg:generic/bref-layer/php-83-fpm@51?arn=arn:aws:lambda:us-east-1:534081306603:layer:php-83-fpm:51", purl_type="generic",
                package="bref-layer/php-83-fpm", version="51", fixed_version="52", relation="layer", path="functions/serverless.yml")
    text = "functions:\n  web:\n    layers:\n      - arn:aws:lambda:us-east-1:534081306603:layer:php-83-fpm:51\n  worker:\n    layers:\n      - arn:aws:lambda:us-east-1:534081306603:layer:php-83:51\n"
    edit = fixes.apply(f, text)
    assert edit.after == text.replace("php-83-fpm:51", "php-83-fpm:52") and "php-83:51" in edit.after and edit.description == "layer php-83-fpm:51 → php-83-fpm:52"
    tf = 'resource "aws_lambda_function" "api" {\n  layers = ["arn:aws:lambda:eu-west-1:534081306603:layer:php-83-fpm:51"]\n}\n'
    assert fixes.apply(dict(f, path="infra/main.tf"), tf).after == tf.replace(":51", ":52")


def test_each_manifest_moves_its_direct_dependency_and_keeps_the_files_shape():
    composer = json.dumps({"require": {"php": "^8.3", "laravel/framework": "^11.2.0"}, "require-dev": {"phpunit/phpunit": "^11"}}, indent=4)
    f = finding(purl="pkg:composer/laravel/framework?constraint=%5E11.2.0", purl_type="composer", package="laravel/framework", version="11.2.0", fixed_version="11.2.1",
                relation="direct", path="composer.json")
    edit = fixes.apply(f, composer)
    assert json.loads(edit.after)["require"]["laravel/framework"] == "^11.2.1" and edit.notes == []
    assert edit.after.count("\n") == composer.count("\n") and edit.description == "laravel/framework 11.2.0 → 11.2.1"
    package = '{\n  "dependencies": {\n    "express": "4.17.1",\n    "@aws-sdk/client-s3": "~3.600.0"\n  }\n}\n'
    assert fixes.apply(dict(f, purl_type="npm", package="express", version="4.17.1", fixed_version="4.19.2", path="package.json"), package).after == package.replace('"4.17.1"', '"4.19.2"')
    assert fixes.apply(dict(f, purl_type="npm", package="@aws-sdk/client-s3", version="3.600.0", fixed_version="3.601.0", path="package.json"), package).after == package.replace("~3.600.0", "~3.601.0")
    reqs = "requests==2.31.0  # http\nDjango[argon2]>=5.0,<5.1 ; python_version > '3.9'\nboto3\n"
    pyf = dict(f, purl_type="pypi", package="django", version="5.0.6", fixed_version="5.0.7", path="requirements.txt")
    assert fixes.apply(pyf, reqs).after == "requests==2.31.0  # http\nDjango[argon2]==5.0.7 ; python_version > '3.9'\nboto3\n"
    assert fixes.apply(dict(pyf, package="boto3", version="1.34.0", fixed_version="1.35.0"), reqs).after.endswith("boto3==1.35.0\n")
    pyproject = '[project]\ndependencies = ["fastapi>=0.115", "PyJWT==2.9.0"]\n[tool.poetry.dependencies]\nhttpx = {version = "0.27.0", extras = ["http2"]}\nrequests = "^2.31"\n'
    assert '"PyJWT==2.9.1"' in fixes.apply(dict(pyf, package="pyjwt", version="2.9.0", fixed_version="2.9.1", path="pyproject.toml"), pyproject).after
    assert 'httpx = {version = "0.27.2"' in fixes.apply(dict(pyf, package="httpx", version="0.27.0", fixed_version="0.27.2", path="pyproject.toml"), pyproject).after
    assert 'requests = "2.32.0"' in fixes.apply(dict(pyf, package="requests", version="2.31.0", fixed_version="2.32.0", path="pyproject.toml"), pyproject).after
    gomod = "module acme/api\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.9.0\n\tgolang.org/x/net v0.27.0 // indirect\n)\n"
    assert fixes.apply(dict(f, purl_type="golang", package="github.com/gin-gonic/gin", version="v1.9.0", fixed_version="v1.10.0", path="go.mod"), gomod).after == gomod.replace("v1.9.0", "v1.10.0")
    cargo = '[dependencies]\nserde = { version = "1.0.203", features = ["derive"] }\ntokio = "1.38.0"\n'
    assert fixes.apply(dict(f, purl_type="cargo", package="tokio", version="1.38.0", fixed_version="1.38.1", path="Cargo.toml"), cargo).after == cargo.replace("1.38.0", "1.38.1")
    assert fixes.apply(dict(f, purl_type="cargo", package="serde", version="1.0.203", fixed_version="1.0.210", path="Cargo.toml"), cargo).after == cargo.replace("1.0.203", "1.0.210")
    assert fixes.strategy_for(dict(f, relation="transitive")) is None and fixes.strategy_for(dict(f, relation="direct", purl_type="gem")) is None


def test_edits_on_one_file_fold_in_sequence():
    text = "FROM php:8.3.30-fpm\nRUN apt-get install -y php8.3-fpm nginx\n"
    first = fixes.apply(finding(path="Dockerfile"), text)
    second = fixes.apply(finding(purl="pkg:deb/debian/php8.3-fpm?distro=debian", purl_type="deb", package="php8.3", version=None, fixed_version="8.3.36-1", relation="package", path="Dockerfile"), first.after)
    folded = fixes.group_by_file([first, second])["Dockerfile"]
    assert folded.before == text and folded.after == "FROM php:8.3.36-fpm\nRUN apt-get install -y php8.3-fpm=8.3.36-1 nginx\n" and folded.strategy == "mixed"
    assert folded.description == "FROM php:8.3.30-fpm → php:8.3.36-fpm; php8.3-fpm → php8.3-fpm=8.3.36-1"
