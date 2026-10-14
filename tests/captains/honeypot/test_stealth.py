"""Nothing AWS or `terraform plan` can show contains a product word (HP-03)."""
from __future__ import annotations

import json
import re

import pytest

from captains.honeypot.honeytoken_material import generate_material
from captains.honeypot.pr_writer import HoneytokensFile, render_honeytokens_tf
from captains.honeypot.terraform_generator import (
    account_module_files,
    generate_terraform,
    module_slug,
    root_outputs,
    stealth_tag,
)

# "cana" + "ry": the vocabulary guard forbids the literal word in this directory.
FORBIDDEN = re.compile("honeypot|honeytoken|cana" + "ry|beamreach|compass|hp[-_]", re.IGNORECASE)
QUEUE_ARN = "arn:aws:sqs:us-east-1:111111111111:alerts"

TOKENS = {
    "aws_credential": {"name": "svc_backup", "resource_path": ""},
    "s3_object": {"name": "backup_env", "resource_path": "corp-backups/secrets/prod.env"},
    "api_token": {"name": "billing_gw", "resource_path": ""},
    "dns_record": {"name": "vault_int", "resource_path": "Z0123456789ABC"},
    "ssh_key": {"name": "deploy_box", "resource_path": ""},
    "db_credential": {"name": "db_backup", "resource_path": ""},
}


def _token(cls, **over):
    return {
        "honeytoken_id": f"11111111-2222-3333-4444-{abs(hash(cls)) % 10**12:012d}",
        "honeytoken_class": cls,
        "workspace_id": "ws-test",
        **TOKENS[cls],
        **over,
    }


def _rendered(token) -> str:
    doc = HoneytokensFile()
    doc.upsert_account(QUEUE_ARN)
    doc.upsert_token(module_slug(token), queue_arn=QUEUE_ARN, workspace_id="ws-test", outputs=root_outputs(token))
    files = {**generate_terraform(token), **account_module_files()}
    return "\n".join([*files.values(), render_honeytokens_tf(doc), *files]).replace("\r", "")


@pytest.mark.parametrize("cls", sorted(TOKENS))
def test_no_product_word_in_anything_rendered(cls):
    text = _rendered(_token(cls))
    match = FORBIDDEN.search(text)
    assert match is None, text[max(0, match.start() - 80): match.end() + 80]


@pytest.mark.parametrize("cls", sorted(TOKENS))
def test_product_word_in_the_given_name_is_replaced_by_a_cover_name(cls):
    token = _token(cls, name="honeypot-" + "cana" + "ry-beamreach")
    assert FORBIDDEN.search(_rendered(token)) is None
    assert FORBIDDEN.search(module_slug(token)) is None


def test_stealth_tag_is_opaque_and_stable():
    key, value = stealth_tag("abc")
    assert key == "cost-center" and re.fullmatch(r"[0-9a-f]{8}", value)
    assert stealth_tag("abc") == (key, value) and stealth_tag("abd")[1] != value


@pytest.mark.parametrize("cls", ["aws_credential", "s3_object", "ssh_key", "api_token", "dns_record", "db_credential"])
def test_only_the_neutral_tag_is_applied(cls):
    token = _token(cls)
    text = generate_terraform(token)[f"services/{module_slug(token)}/main.tf"]
    key, value = stealth_tag(token["honeytoken_id"])
    assert f'"{key}" = "{value}"' in text
    assert text.count("tags = {") == text.count(f'"{key}" = "{value}"')


def test_aws_credential_material_has_no_invented_key():
    material = generate_material(honeytoken_class="aws_credential", name="svc_backup", region="eu-west-1")
    inner = material["material"]
    assert inner["access_key_id"] is None and inner["secret_access_key"] is None
    assert inner["status"] == "pending_apply"
    assert "AWS_ACCESS_KEY_ID" in inner["env_vars"]
    assert "AKIA" not in str(material)
    assert material["snippets"]


def test_s3_object_content_matches_the_material():
    material = generate_material(honeytoken_class="s3_object", name="backup_env")
    token = _token("s3_object", material=material)
    main = generate_terraform(token)[f"services/{module_slug(token)}/main.tf"]
    import hcl2

    parsed = hcl2.loads(main)
    obj = next(iter(parsed["resource"][0].values()))
    body = next(iter(obj.values()))
    assert json.loads(body["content"]) == material["material"]["content"] == material["material"]["preview"]
    assert "change_me" not in main
