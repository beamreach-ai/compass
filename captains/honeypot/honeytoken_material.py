"""Generates the *material* for each honeytoken class.

A honeytoken's material is the fake, valuable-looking artifact an attacker
discovers and uses: a fake AWS access key, an API token, a passwords file, an
SSH key, a hostname. This module produces the material we control directly at
creation time, plus ready-to-paste snippets that tell the user *how* to plant
it. For classes whose real cryptographic material is minted by Terraform
(`aws_credential`, `ssh_key`), the secret is emitted as a Terraform output
after apply; here we generate a realistic-looking placeholder and point the
user at that output.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import string
from typing import Any, Dict, Optional

from captains.honeypot.classes import normalize_class

# ── Public shape ────────────────────────────────────────────────────────────
#
# generate_material(...) -> {
#   "material": {..public, non-secret display values..},
#   "secret_ref": "terraform_output" | "inline",   # where the real secret lives
#   "planted_by": "terraform" | "user",            # who places the trap
#   "instructions": "human guidance",
#   "snippets": [ {"label", "language", "content"} ],
# }


# Realistic cover names. A honeytoken is named after something a service
# account would plausibly be called, never after what it is. The pick is a hash
# of the honeytoken id, so the same token always gets the same name.
COVER_NAME_POOL = (
    "svc-backup-reporting",
    "ci-artifacts-reader",
    "svc-metrics-export",
    "data-sync-ro",
)


def default_cover_name(seed: str = "") -> str:
    """A realistic service-account name, unique per seed.

    The pool has a handful of entries, so two honeytokens in one account would
    otherwise share a name (and a module directory, and an IAM user). A short
    hash suffix keeps each one distinct while still looking like a real
    service account.
    """
    digest = hashlib.sha256(str(seed or "").encode()).digest()
    base = COVER_NAME_POOL[digest[0] % len(COVER_NAME_POOL)]
    return f"{base}-{digest[1:3].hex()}" if seed else base


def cover_label(name: str) -> str:
    """Hyphenated, lower-case form of a name, as it appears in AWS (`svc_backup` -> `svc-backup`)."""
    return re.sub(r"[^a-z0-9-]", "-", str(name).lower().replace("_", "-"))[:48].strip("-")


_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def _rand_b32(n: int) -> str:
    return "".join(secrets.choice(_B32) for _ in range(n))


def _rand_secret(n: int = 40) -> str:
    raw = base64.b64encode(secrets.token_bytes(n)).decode()
    return raw[:n]


def s3_object_content() -> str:
    """Body of the planted S3 object. The generator writes exactly what the
    material shows, so the same string must flow through both."""
    return (
        "# Production database credentials\n"
        "DB_HOST=prod-db.internal\n"
        "DB_USER=svc-backup\n"
        f"DB_PASSWORD={_rand_secret(24)}\n"
        "DB_NAME=payments\n"
    )


def _token(prefix: str, n: int = 32) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return f"{prefix}_{''.join(secrets.choice(alphabet) for _ in range(n))}"


def generate_material(
    *,
    honeytoken_class: str,
    name: str,
    region: Optional[str] = None,
    receiver_url: Optional[str] = None,
) -> Dict[str, Any]:
    generators = {
        "s3_object": _material_s3_object,
        "aws_credential": _material_aws_credential,
        "api_token": _material_api_token,
        "dns_record": _material_dns_record,
        "ssh_key": _material_ssh_key,
        "db_credential": _material_db_credential,
    }
    cls = normalize_class(honeytoken_class)
    generator = generators.get(cls)
    if not generator:
        raise ValueError(f"Unknown honeytoken class: {honeytoken_class}")
    return generator(name=name, region=region or "us-east-1", receiver_url=receiver_url)


# ── S3 object ────────────────────────────────────────────────────────

def _material_s3_object(*, name: str, region: str, receiver_url: Optional[str]) -> Dict[str, Any]:
    content = s3_object_content()
    return {
        # `content` is what the plant PR writes into the object, byte for byte.
        "material": {"filename": f"{cover_label(name)}.env", "preview": content, "content": content},
        "secret_ref": "inline",
        "planted_by": "terraform",
        "instructions": (
            "This file is deployed into the target S3 bucket by the generated PR. "
            "It looks like leaked database credentials; any read of it triggers an alert. "
            "You do not need to plant anything manually."
        ),
        "snippets": [
            {"label": "File content", "language": "bash", "content": content},
        ],
    }


# ── AWS credential (access key) ───────────────────────────────────────────────────

def _material_aws_credential(*, name: str, region: str, receiver_url: Optional[str]) -> Dict[str, Any]:
    # The key id is minted by Terraform at apply time; never invent one here.
    creds_ini = (
        "[default]\n"
        "aws_access_key_id = <access key id, filled in after apply>\n"
        "aws_secret_access_key = <secret access key, from the module output>\n"
    )
    env_block = (
        "AWS_ACCESS_KEY_ID=<access key id, filled in after apply>\n"
        "AWS_SECRET_ACCESS_KEY=<secret access key, from the module output>\n"
        f"AWS_DEFAULT_REGION={region}\n"
    )
    return {
        "material": {
            "access_key_id": None,
            "secret_access_key": None,
            "status": "pending_apply",
            "region": region,
            "env_vars": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"],
        },
        "secret_ref": "terraform_output",
        "planted_by": "user",
        "instructions": (
            "After the PR is applied, the real access key id and secret are available from "
            "the module's Terraform outputs. Plant them somewhere an attacker would look: a "
            "wiki page, CI variables, a git repo, or a developer laptop. Any AWS API call "
            "with this key fires a critical alert. Record where you plant it as its "
            "placement so we can pinpoint the breach source when it fires."
        ),
        "snippets": [
            {"label": "~/.aws/credentials", "language": "ini", "content": creds_ini},
            {"label": ".env", "language": "bash", "content": env_block},
        ],
    }


# ── Fake API token (callback-based) ─────────────────────────────────────────

def _material_api_token(*, name: str, region: str, receiver_url: Optional[str]) -> Dict[str, Any]:
    token = _token("brk_live", 32)
    url = receiver_url or "<receiver URL from Terraform output after apply>"
    curl = (
        f"# Internal Payments API — service token\n"
        f"curl -H 'Authorization: Bearer {token}' \\\n"
        f"     {url}/v1/charges\n"
    )
    doc = (
        "## Payments service (internal)\n\n"
        f"Base URL: {url}\n"
        f"Auth: `Authorization: Bearer {token}`\n"
    )
    return {
        "material": {"token": token, "callback_url": url},
        "secret_ref": "inline",
        "planted_by": "user",
        "instructions": (
            "Embed this token in a place an attacker would harvest it — a fake internal "
            "API doc, a config file, source code, or a Postman collection. Any use of the "
            "token calls back to the receiver and fires an alert. Record where you plant it "
            "as its placement."
        ),
        "snippets": [
            {"label": "API doc snippet", "language": "markdown", "content": doc},
            {"label": "curl example", "language": "bash", "content": curl},
        ],
    }


# ── DNS record ───────────────────────────────────────────────────────

def _material_dns_record(*, name: str, region: str, receiver_url: Optional[str]) -> Dict[str, Any]:
    hostname = f"{cover_label(name)}-internal.corp.local"
    return {
        "material": {"hostname": hostname},
        "secret_ref": "inline",
        "planted_by": "user",
        "instructions": (
            "Reference this hostname somewhere an attacker doing reconnaissance would find "
            "it — a config file, a comment, or internal docs. Any DNS lookup of it is logged "
            "and alerts. Internal resolver queries indicate lateral movement."
        ),
        "snippets": [
            {"label": "Config reference", "language": "yaml", "content": f"internal_api_host: {hostname}\n"},
        ],
    }


# ── Fake SSH key ────────────────────────────────────────────────────────────

def _material_ssh_key(*, name: str, region: str, receiver_url: Optional[str]) -> Dict[str, Any]:
    return {
        "material": {"key_name": cover_label(name)},
        "secret_ref": "terraform_output",
        "planted_by": "user",
        "instructions": (
            "After the PR is applied, download the private key from the Terraform output "
            "(the module's `private_key` output) and plant it as an `id_rsa` on a bastion, in a "
            "backup archive, or in a repo. Any authentication attempt against the honey "
            "endpoint using it fires a critical alert. Record where you plant it under "
            "Placements."
        ),
        "snippets": [
            {"label": "Retrieve private key", "language": "bash",
             "content": "terraform output -raw private_key > id_rsa\nchmod 600 id_rsa\n"},
        ],
    }


# ── Decoy database credential ────────────────────────────────────────

DB_NAMESPACE = "internal"
DB_USER = "svc_backup"


def _material_db_credential(*, name: str, region: str, receiver_url: Optional[str]) -> Dict[str, Any]:
    host = f"{cover_label(name)}.{DB_NAMESPACE}"
    password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
    env_block = f"DB_HOST={host}\nDB_USER={DB_USER}\nDB_PASSWORD={password}\n"
    return {
        "material": {
            "host": host,
            "user": DB_USER,
            "password": password,
            "env_vars": ["DB_HOST", "DB_USER", "DB_PASSWORD"],
            "status": "ready",
        },
        "secret_ref": "inline",
        "planted_by": "user",
        "instructions": (
            "The generated PR deploys a small database endpoint at this host. Plant these values "
            "where an attacker would look (a task definition's environment, a .env file, a wiki "
            "page). Any login attempt against the host fires a critical alert; a bare connection "
            "is recorded as a low-severity probe."
        ),
        "snippets": [
            {"label": ".env", "language": "bash", "content": env_block},
            {"label": "psql", "language": "bash", "content": f"psql \"host={host} user={DB_USER}\"\n"},
        ],
    }
