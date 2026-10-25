"""The world the end-to-end suite runs Compass against: one AWS account (moto) and one
GitHub organisation (e2e/fakes) with an infra repository whose Terraform manages part
of the account.

What is in it, and why, is what the tests assert on:

    managed     in the repository's Terraform state and in the account: covered by IaC
    drifted     managed, but the live value differs from the state (the queue's
                visibility timeout): the drift overlay and fix-drift have one to show
    unmanaged   in the account, in no state: the IaC tab offers to import them
    decoy bait  a secret and a role a decoy can sit next to

Everything is created in us-east-1, the region `init_config` records, so the expected
counts do not depend on how many regions moto lists.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any, Dict

ACCOUNT = "123456789012"
REGION = "us-east-1"
ORG = "acme"
REPO = "acme/infra"
TERRAFORM_ROOT = "environments/prod"
STATE_BUCKET = "acme-tfstate"
STATE_KEY = "prod/terraform.tfstate"

MANAGED_BUCKET = "acme-app-assets"
UNMANAGED_BUCKET = "acme-legacy-exports"
MANAGED_QUEUE = "acme-jobs"            # drifted: state says 30, the account 60
UNMANAGED_QUEUE = "acme-adhoc"
MANAGED_ROLE = "acme-api-role"
MANAGED_FUNCTION = "acme-api"
SECRET = "acme/prod/db-password"

DECLARED_VISIBILITY = 30
LIVE_VISIBILITY = 60


def arn(service: str, resource: str, *, region: str = REGION) -> str:
    if service in ("iam", "s3"):
        return f"arn:aws:{service}::{ACCOUNT if service == 'iam' else ''}:{resource}"
    return f"arn:aws:{service}:{region}:{ACCOUNT}:{resource}"


def queue_url(name: str, endpoint: str) -> str:
    return f"{endpoint.rstrip('/')}/{ACCOUNT}/{name}"


# ── the account ─────────────────────────────────────────────────────────────

def seed_aws(session: Any) -> Dict[str, str]:
    """Create the account's resources through ``session`` (boto3, pointed at moto). Idempotent
    enough to run once per stack; returns the ids the tests refer to."""
    s3 = session.client("s3", region_name=REGION)
    for bucket in (STATE_BUCKET, MANAGED_BUCKET, UNMANAGED_BUCKET):
        s3.create_bucket(Bucket=bucket)
    sqs = session.client("sqs", region_name=REGION)
    managed_queue = sqs.create_queue(QueueName=MANAGED_QUEUE, Attributes={"VisibilityTimeout": str(LIVE_VISIBILITY)})["QueueUrl"]
    sqs.create_queue(QueueName=UNMANAGED_QUEUE)
    iam = session.client("iam", region_name=REGION)
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    role_arn = iam.create_role(RoleName=MANAGED_ROLE, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("handler.py", "def handler(event, context):\n    return {'ok': True}\n")
    function_arn = session.client("lambda", region_name=REGION).create_function(
        FunctionName=MANAGED_FUNCTION, Runtime="python3.12", Role=role_arn, Handler="handler.handler",
        Code={"ZipFile": package.getvalue()}, MemorySize=256, Timeout=30,
    )["FunctionArn"]
    secret_arn = session.client("secretsmanager", region_name=REGION).create_secret(Name=SECRET, SecretString="not-a-real-password")["ARN"]
    s3.put_object(Bucket=STATE_BUCKET, Key=STATE_KEY, Body=json.dumps(terraform_state(managed_queue, role_arn, function_arn)).encode())
    return {"queue_url": managed_queue, "role_arn": role_arn, "function_arn": function_arn, "secret_arn": secret_arn}


def terraform_state(queue: str, role_arn: str, function_arn: str) -> Dict[str, Any]:
    """The Terraform state the repository's backend points at: what Terraform believes it manages."""

    def resource(kind: str, name: str, attributes: Dict[str, Any]) -> Dict[str, Any]:
        return {"mode": "managed", "type": kind, "name": name, "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
                "instances": [{"schema_version": 0, "attributes": attributes}]}

    return {
        "version": 4, "terraform_version": "1.5.7", "serial": 7, "lineage": "e2e-fixture", "outputs": {},
        "resources": [
            resource("aws_s3_bucket", "assets", {"id": MANAGED_BUCKET, "bucket": MANAGED_BUCKET, "arn": f"arn:aws:s3:::{MANAGED_BUCKET}"}),
            resource("aws_s3_bucket", "state", {"id": STATE_BUCKET, "bucket": STATE_BUCKET, "arn": f"arn:aws:s3:::{STATE_BUCKET}"}),
            resource("aws_sqs_queue", "jobs", {"id": queue, "name": MANAGED_QUEUE, "url": queue, "arn": arn("sqs", MANAGED_QUEUE),
                                               "visibility_timeout_seconds": DECLARED_VISIBILITY}),
            resource("aws_iam_role", "api", {"id": MANAGED_ROLE, "name": MANAGED_ROLE, "arn": role_arn}),
            resource("aws_lambda_function", "api", {"id": MANAGED_FUNCTION, "function_name": MANAGED_FUNCTION, "arn": function_arn,
                                                    "memory_size": 256, "timeout": 30, "runtime": "python3.12", "role": role_arn}),
        ],
    }


# ── the repository ──────────────────────────────────────────────────────────

MAIN_TF = f'''terraform {{
  backend "s3" {{
    bucket = "{STATE_BUCKET}"
    key    = "{STATE_KEY}"
    region = "{REGION}"
  }}
}}

provider "aws" {{
  region = "{REGION}"
}}

resource "aws_s3_bucket" "assets" {{
  bucket = "{MANAGED_BUCKET}"
}}

resource "aws_s3_bucket" "state" {{
  bucket = "{STATE_BUCKET}"
}}

resource "aws_sqs_queue" "jobs" {{
  name                       = "{MANAGED_QUEUE}"
  visibility_timeout_seconds = {DECLARED_VISIBILITY}
}}

resource "aws_iam_role" "api" {{
  name               = "{MANAGED_ROLE}"
  assume_role_policy = data.aws_iam_policy_document.lambda.json
}}

data "aws_iam_policy_document" "lambda" {{
  statement {{
    actions = ["sts:AssumeRole"]
    principals {{
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }}
  }}
}}

resource "aws_lambda_function" "api" {{
  function_name = "{MANAGED_FUNCTION}"
  role          = aws_iam_role.api.arn
  runtime       = "python3.12"
  handler       = "handler.handler"
  memory_size   = 256
  timeout       = 30
  filename      = "build/api.zip"
}}
'''

WEB_DOCKERFILE = "services/web/Dockerfile"
WEB_DOCKERFILE_TEXT = "FROM php:8.3.30-fpm\nRUN apt-get update && apt-get install -y --no-install-recommends libzip-dev && rm -rf /var/lib/apt/lists/*\nCOPY . /var/www\n"
WEB_COMPOSER_JSON = "services/web/composer.json"
WEB_COMPOSER_LOCK = "services/web/composer.lock"

REPO_FILES = {
    f"{TERRAFORM_ROOT}/main.tf": MAIN_TF,
    # a PHP service the patch captain has something to fix in (4.16): a docker-library base image and a composer project
    WEB_DOCKERFILE: WEB_DOCKERFILE_TEXT,
    WEB_COMPOSER_JSON: '{\n    "require": {\n        "php": "^8.3",\n        "laravel/framework": "^11.2.0"\n    }\n}\n',
    WEB_COMPOSER_LOCK: '{"packages": [{"name": "laravel/framework", "version": "v11.2.0"}, {"name": "symfony/http-foundation", "version": "v7.1.3"}], "packages-dev": []}\n',
    "README.md": "# acme/infra\n\nThe end-to-end suite's infrastructure repository.\n",
    ".github/workflows/plan.yml": "name: plan\non: [pull_request]\njobs:\n  plan:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n",
}


def seed_github(fakes: Any) -> None:
    """Create the infra repository in the fake GitHub (``fakes`` is an httpx client on it)."""
    fakes.put(f"/_control/repos/{REPO}", json={"files": REPO_FILES}).raise_for_status()
