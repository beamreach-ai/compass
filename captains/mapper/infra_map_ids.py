from __future__ import annotations

import hashlib
from typing import Optional
from urllib.parse import quote


def github_repo_node_id(full_name: str) -> str:
    return f"github://repo/{str(full_name).strip().lower()}"


def github_file_node_id(full_name: str, path: str) -> str:
    repo = str(full_name).strip().lower()
    normalized_path = str(path).strip().lstrip("/")
    return f"github://file/{repo}/{quote(normalized_path, safe='/._-')}"


def tf_root_module_node_id(full_name: str, path: str) -> str:
    repo = str(full_name).strip().lower()
    normalized_path = str(path or ".").strip().strip("/") or "."
    return f"tfroot://github/{repo}/{quote(normalized_path, safe='/._-')}"


def tf_state_file_node_id(bucket: str, key: str) -> str:
    normalized_key = str(key).strip().lstrip("/")
    return f"tfstate://s3/{bucket}/{normalized_key}"


def tf_address_node_id(tf_root_module_id: str, terraform_address: str) -> str:
    encoded_root = quote(str(tf_root_module_id), safe="")
    return f"tfaddr://{encoded_root}#{terraform_address}"


def tf_state_instance_node_id(
    tf_state_file_id: str,
    terraform_address: str,
    *,
    instance_key: Optional[str] = None,
    aws_resource_id: Optional[str] = None,
) -> str:
    raw = "|".join(
        [
            tf_state_file_id,
            terraform_address,
            str(instance_key or ""),
            str(aws_resource_id or ""),
        ]
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return f"tfstate-instance://{digest}"


def resource_scope(resource_id: str) -> str:
    return f"resource:{resource_id}"


def state_file_scope(tf_state_file_id: str) -> str:
    return f"state_file:{tf_state_file_id}"


def repo_scope(full_name: str) -> str:
    return f"repo:{str(full_name).strip().lower()}"


def tf_root_scope(tf_root_module_id: str) -> str:
    return f"tf_root_module:{tf_root_module_id}"
