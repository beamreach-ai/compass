"""The decoy database module: parses, exposes the right outputs, validates."""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import hcl2
import pytest

from captains.honeypot.honeytoken_material import generate_material
from captains.honeypot.terraform_generator import generate_terraform, module_slug

QUEUE_ARN = "arn:aws:sqs:us-east-1:111111111111:alerts"
FORBIDDEN = re.compile("honeypot|honeytoken|cana" + "ry|beamreach|compass|hp[-_]", re.IGNORECASE)


def _token():
    material = generate_material(honeytoken_class="db_credential", name="db_backup")
    return {
        "honeytoken_id": "ht-db",
        "honeytoken_class": "db_credential",
        "workspace_id": "ws-test",
        "name": "db_backup",
        "material": material,
    }, material


def _files():
    token, material = _token()
    return token, material, generate_terraform(token)


def test_material_shape():
    _, material = _token()
    inner = material["material"]
    assert inner["host"] == "db-backup.internal" and inner["user"] == "svc_backup"
    assert len(inner["password"]) == 24 and inner["status"] == "ready"
    assert inner["env_vars"] == ["DB_HOST", "DB_USER", "DB_PASSWORD"] and material["snippets"]


def test_files_parse_and_expose_outputs():
    token, material, files = _files()
    slug = module_slug(token)
    assert set(files) == {f"services/{slug}/main.tf", f"services/{slug}/variables.tf"}
    parsed = hcl2.loads(files[f"services/{slug}/main.tf"])
    outputs = {k.strip('"') for o in parsed["output"] for k in o}
    assert outputs == {"host", "user", "password"}
    assert next(o['"password"'] for o in parsed["output"] if '"password"' in o)["sensitive"] in (True, "true")
    assert material["material"]["password"] in files[f"services/{slug}/main.tf"]


def test_task_role_may_only_send_to_the_queue_and_sg_is_vpc_only():
    token, _, files = _files()
    main = files[f"services/{module_slug(token)}/main.tf"]
    assert main.count("sqs:SendMessage") == 1 and "sqs:*" not in main
    assert "Resource = var.alert_queue_arn" in main
    assert "data.aws_vpc.this.cidr_block" in main and "from_port   = 5432" in main
    for env in ("TOKEN_ID", "WORKSPACE_ID", "ALERT_QUEUE_URL"):
        assert f'name = "{env}"' in main
    assert "aws_vpc_endpoint" not in main


def test_no_product_word():
    _, _, files = _files()
    text = "\n".join([*files.values(), *files])
    assert FORBIDDEN.search(text) is None


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform binary not on PATH")
def test_terraform_validate(tmp_path):
    token, _, files = _files()
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    slug = module_slug(token)
    (tmp_path / "root.tf").write_text(
        f'''provider "aws" {{
  region = "us-east-1"
  skip_credentials_validation = true
  skip_requesting_account_id = true
  skip_metadata_api_check = true
  access_key = "x"
  secret_key = "x"
}}

module "svc_{slug}" {{
  source                  = "./services/{slug}"
  alert_queue_arn         = "{QUEUE_ARN}"
  alert_delivery_role_arn = "arn:aws:iam::111111111111:role/x"
  workspace_id            = "ws-test"
  vpc_id                  = "vpc-123"
  subnet_ids              = ["subnet-1"]
  image                   = "111111111111.dkr.ecr.us-east-1.amazonaws.com/db-backup:latest"
}}
'''
    )
    env = {**os.environ, "TF_IN_AUTOMATION": "1"}
    init = subprocess.run(["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=600)
    if init.returncode != 0 and "dial" in init.stderr.lower():
        pytest.skip(f"cannot download providers: {init.stderr[:200]}")
    assert init.returncode == 0, init.stdout + init.stderr
    validate = subprocess.run(["terraform", "validate", "-no-color"], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=120)
    assert validate.returncode == 0, validate.stdout + validate.stderr
