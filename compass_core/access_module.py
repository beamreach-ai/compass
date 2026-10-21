"""The access module: the two Compass roles as Terraform, regenerated whole from the
manifests, the grants and the config (docs/specs/access-model.md §9.2, D1).

``services/_ops_access/main.tf`` holds the read role with its generated policy, the act
role with the union of the enabled grants' statements, and both trust policies. Every
pull request renders the module as it should be now, never a hand-merged diff, so two
grants accepted in the same minute land in one pull request and a captain install and
an action grant are the same kind of change. ``imports.tf`` carries ``import`` blocks
for the two roles `connect` created, so the first pull request brings them under code.

The module is wired from a root file of its own, ``access.tf``, next to the decoy
captain's ``services.tf``; the core does not edit a captain's file.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from compass_core import permissions
from compass_core.aws import _aws_config, control_config, external_id, roles

MODULE_DIR = "services/_ops_access"
MODULE_NAME = "svc_ops_access"
ROOT_FILE = "access.tf"
READ_POLICY_NAME = "inventory-read"
ACT_POLICY_NAME = "ops"


def act_policy(grants: Iterable[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    """The union of the statements of every grant the module carries (pr_open … armed)."""
    from compass_core import grants as _grants

    statements: List[Dict[str, Any]] = []
    seen = set()
    for row in grants:
        if not _grants.is_granted(row):
            continue
        for statement in _grants.statements_for(row, config):
            key = json.dumps(statement, sort_keys=True)
            if key not in seen:
                seen.add(key)
                statements.append(statement)
    return {"Version": "2012-10-17", "Statement": statements}


def render(config: Dict[str, Any], grants: Iterable[Dict[str, Any]], *, manifests: Optional[Iterable[Any]] = None,
           with_imports: bool = False) -> Dict[str, Any]:
    """The module's files, and the documents they hold, for this workspace."""
    from compass_core import captains as registry
    from compass_core.connect import ROLE_PATH, TAG, trust_policy

    aws = _aws_config(config)
    role_arns = roles(config)
    read_name = str(aws.get("read_role_name") or str(role_arns.get("read") or "").rsplit("/", 1)[-1] or "svc-inventory-reader")
    act_name = str(aws.get("act_role_name") or str(role_arns.get("act") or "").rsplit("/", 1)[-1] or "svc-ops-responder")
    base = str(control_config(config).get("runtime_arn") or "")
    trust = trust_policy(base, external_id(config) or "")
    read_policy = permissions.read_role_policy(list(manifests) if manifests is not None else registry.manifests(), config)
    act = act_policy(grants, config)
    prefix = str(aws.get("session_prefix") or "compass")
    tags = f'  tags = {{ "{TAG}" = "{prefix}" }}\n'
    main_tf = (
        "# Compass access: the read role and the act role. Generated from the installed captains'\n"
        "# manifests and the enabled actions; edit those in Compass, not this file.\n\n"
        f'resource "aws_iam_role" "read" {{\n  name = "{read_name}"\n  path = "{ROLE_PATH}"\n'
        f"  assume_role_policy = jsonencode({json.dumps(trust, indent=2)})\n{tags}}}\n\n"
        f'resource "aws_iam_role_policy" "read" {{\n  name   = "{READ_POLICY_NAME}"\n  role   = aws_iam_role.read.id\n'
        f"  policy = jsonencode({json.dumps(read_policy, indent=2)})\n}}\n\n"
        f'resource "aws_iam_role" "act" {{\n  name = "{act_name}"\n  path = "{ROLE_PATH}"\n'
        f"  assume_role_policy = jsonencode({json.dumps(trust, indent=2)})\n{tags}}}\n"
    )
    if act["Statement"]:
        main_tf += (
            f'\nresource "aws_iam_role_policy" "act" {{\n  name   = "{ACT_POLICY_NAME}"\n  role   = aws_iam_role.act.id\n'
            f"  policy = jsonencode({json.dumps(act, indent=2)})\n}}\n"
        )
    main_tf += '\noutput "read_role_arn" {\n  value = aws_iam_role.read.arn\n}\n\noutput "act_role_arn" {\n  value = aws_iam_role.act.arn\n}\n'
    files = {f"{MODULE_DIR}/main.tf": main_tf}
    if with_imports:
        files[f"{MODULE_DIR}/imports.tf"] = (
            "# The roles `compass connect` created, brought under code. Terraform 1.5+; safe to keep.\n"
            f'import {{\n  to = aws_iam_role.read\n  id = "{read_name}"\n}}\n\nimport {{\n  to = aws_iam_role.act\n  id = "{act_name}"\n}}\n'
        )
    root = (
        "# Compass access module. Generated; manual edits may be overwritten.\n"
        f'module "{MODULE_NAME}" {{\n  source = "./{MODULE_DIR}"\n}}\n\n'
        f'output "compass_read_role_arn" {{\n  value = module.{MODULE_NAME}.read_role_arn\n}}\n\n'
        f'output "compass_act_role_arn" {{\n  value = module.{MODULE_NAME}.act_role_arn\n}}\n'
    )
    return {
        "module_dir": MODULE_DIR, "module_name": MODULE_NAME, "root_file": ROOT_FILE,
        "files": files, "root": root,
        "read_policy": read_policy, "act_policy": act, "trust": trust,
        "read_role": {"name": read_name, "arn": role_arns.get("read")}, "act_role": {"name": act_name, "arn": role_arns.get("act")},
    }
