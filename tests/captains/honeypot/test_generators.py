"""Generated honeytoken modules parse, and validate when loaded from a root."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import hcl2
import pytest

from captains.honeypot.pr_writer import HoneytokensFile, render_honeytokens_tf
from captains.honeypot.terraform_generator import (
    account_module_files,
    generate_terraform,
    module_slug,
    root_outputs,
)

QUEUE_ARN = "arn:aws:sqs:us-east-1:111111111111:compass-alerts"

TOKENS = {
    "aws_credential": {"name": "svc_backup", "resource_path": ""},
    "s3_object": {"name": "backup_env", "resource_path": "corp-backups/secrets/prod.env"},
    "api_token": {"name": "billing_gw", "resource_path": ""},
    "dns_record": {"name": "vault_int", "resource_path": "Z0123456789ABC"},
    "ssh_key": {"name": "deploy_box", "resource_path": ""},
}

PROVIDER = (
    'provider "aws" {\n  region = "us-east-1"\n'
    "  skip_credentials_validation = true\n  skip_requesting_account_id = true\n"
    '  skip_metadata_api_check = true\n  access_key = "x"\n  secret_key = "x"\n}\n'
)


def _token(cls: str) -> dict:
    return {
        "honeytoken_id": f"ht-{cls}",
        "honeytoken_class": cls,
        "workspace_id": "ws-test",
        **TOKENS[cls],
    }


def _write_root(root: pathlib.Path, token: dict) -> None:
    files = {**generate_terraform(token), **account_module_files()}
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    doc = HoneytokensFile()
    doc.upsert_account(QUEUE_ARN)
    doc.upsert_token(
        module_slug(token),
        queue_arn=QUEUE_ARN,
        workspace_id="ws-test",
        outputs=root_outputs(token),
    )
    (root / "services.tf").write_text(render_honeytokens_tf(doc))
    (root / "provider.tf").write_text(PROVIDER)


@pytest.mark.parametrize("cls", sorted(TOKENS))
def test_generated_files_parse_as_hcl(cls):
    token = _token(cls)
    files = generate_terraform(token)
    slug = module_slug(token)
    assert set(files) >= {f"services/{slug}/main.tf", f"services/{slug}/variables.tf"}
    assert not any(p.startswith("honeypots/") for p in files)
    for path, content in {**files, **account_module_files()}.items():
        hcl2.loads(content)  # raises on bad syntax
        assert "var.beamreach_" not in content and 'variable "beamreach_' not in content, path


@pytest.mark.parametrize("cls", sorted(TOKENS))
def test_event_targets_use_delivery_role(cls):
    main = generate_terraform(_token(cls))[f"services/{module_slug(_token(cls))}/main.tf"]
    assert main.count("aws_cloudwatch_event_target") == main.count(
        "role_arn  = var.alert_delivery_role_arn"
    )
    assert "var.beamreach_" not in main


def test_variables_use_neutral_names():
    variables = generate_terraform(_token("aws_credential"))[
        f"services/{module_slug(_token('aws_credential'))}/variables.tf"
    ]
    parsed = hcl2.loads(variables)
    names = {k.strip('"') for b in parsed["variable"] for k in b}
    assert {
        "workspace_id",
        "alert_queue_arn",
        "alert_delivery_role_arn",
    } <= names


def test_root_outputs_never_export_secrets():
    for cls in TOKENS:
        for module_output in root_outputs(_token(cls)):
            assert "secret" not in module_output and "private" not in module_output


@pytest.fixture(scope="session")
def plugin_cache(tmp_path_factory):
    path = tmp_path_factory.mktemp("tf-plugin-cache")
    return str(path)


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform binary not on PATH")
@pytest.mark.parametrize("cls", sorted(TOKENS))
def test_terraform_validate(cls, tmp_path, plugin_cache):
    _write_root(tmp_path, _token(cls))
    env = {**os.environ, "TF_PLUGIN_CACHE_DIR": plugin_cache, "TF_IN_AUTOMATION": "1"}
    init = subprocess.run(
        ["terraform", "init", "-backend=false", "-input=false", "-no-color"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=600,
    )
    if init.returncode != 0 and ("registry" in init.stderr.lower() and "dial" in init.stderr.lower()):
        pytest.skip(f"cannot download providers: {init.stderr[:200]}")
    assert init.returncode == 0, init.stdout + init.stderr
    validate = subprocess.run(
        ["terraform", "validate", "-no-color"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert validate.returncode == 0, validate.stdout + validate.stderr


def _resource_blocks(hcl_text: str, rtype: str) -> dict:
    out = {}
    for block in hcl2.loads(hcl_text).get("resource", []):
        for raw_type, labels in block.items():
            if raw_type.strip('"') == rtype:
                for label, body in labels.items():
                    out[label.strip('"')] = body
    return out


def _unq(value):
    return value[1:-1] if isinstance(value, str) and len(value) > 1 and value[0] == value[-1] == '"' else value


EXPECTED_PATHS = {
    "eventId": "$.detail.eventID",
    "eventTime": "$.detail.eventTime",
    "eventName": "$.detail.eventName",
    "region": "$.detail.awsRegion",
    "accessKeyId": "$.detail.userIdentity.accessKeyId",
    "principalArn": "$.detail.userIdentity.arn",
    "sourceIp": "$.detail.sourceIPAddress",
    "userAgent": "$.detail.userAgent",
    "errorCode": "$.detail.errorCode",
    "vpcEndpointId": "$.detail.vpcEndpointId",
}


@pytest.mark.parametrize("cls", ["aws_credential", "s3_object", "ssh_key"])
def test_rule_targets_emit_the_full_payload(cls):
    token = _token(cls)
    main = generate_terraform(token)[f"services/{module_slug(token)}/main.tf"]
    (target,) = _resource_blocks(main, "aws_cloudwatch_event_target").values()
    transformer = target["input_transformer"][0]
    assert {_unq(k): _unq(v) for k, v in transformer["input_paths"].items()} == EXPECTED_PATHS
    template = transformer["input_template"]
    for key in (
        "token_id", "workspace_id", "event_id", "event_time", "event_name", "region",
        "access_key_id", "principal_arn", "source_ip", "user_agent", "error_code",
        "vpc_endpoint_id",
    ):
        assert f'"{key}"' in template
    assert _unq(target["role_arn"]) == "${var.alert_delivery_role_arn}"


def test_aws_credential_rule_sees_read_only_management_events():
    token = _token("aws_credential")
    main = generate_terraform(token)[f"services/{module_slug(token)}/main.tf"]
    (rule,) = _resource_blocks(main, "aws_cloudwatch_event_rule").values()
    assert _unq(rule["state"]) == "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"
    assert 'state       = "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"' in main
