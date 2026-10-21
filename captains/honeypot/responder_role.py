"""The responder role: an IAM role in the target account generated from the
enabled whitelist rows, so the role can do exactly what the rows allow and
nothing else. Delivered as a Terraform module through the same PR path as
honeytoken plants.

Nothing AWS shows carries a product word: the role is ``svc-ops-responder``
by default and the policy statements are the catalog's own.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from captains.honeypot.actions import get_action
from captains.honeypot.actions.base import ActionScope
from compass_core.grants import context_entries

DEFAULT_ROLE_NAME = "svc-ops-responder"
MODULE_DIR = "services/_responder"
MODULE_NAME = "svc_responder"


def responder_policy(rows: Iterable[Dict[str, Any]], *, account_id: str, region: str) -> Dict[str, Any]:
    """Union of the statements the enabled rows need. Disabled rows add nothing."""
    statements: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        if not row.get("enabled", True):
            continue
        action = get_action(row["action_type"])
        scope = ActionScope.from_dict(row.get("scope"))
        for stmt in action.iam_statements(scope, account_id=account_id, region=region):
            key = json.dumps(stmt, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            statements.append(stmt)
    return {"Version": "2012-10-17", "Statement": statements}


def trust_policy(trust_principal_arns: List[str], external_id: Optional[str]) -> Dict[str, Any]:
    statement: Dict[str, Any] = {
        "Effect": "Allow",
        "Principal": {"AWS": list(trust_principal_arns)},
        "Action": "sts:AssumeRole",
    }
    if external_id:
        statement["Condition"] = {"StringEquals": {"sts:ExternalId": external_id}}
    return {"Version": "2012-10-17", "Statement": [statement]}


def generate_responder_module(
    rows: Iterable[Dict[str, Any]],
    *,
    account_id: str,
    region: str,
    trust_principal_arns: List[str],
    external_id: Optional[str],
    role_name: str = DEFAULT_ROLE_NAME,
) -> Dict[str, Any]:
    rows = list(rows)
    policy = responder_policy(rows, account_id=account_id, region=region)
    trust = trust_policy(trust_principal_arns, external_id)
    main_tf = f'''# Operations responder role (auto-generated; edit the enabled actions
# in the console instead of this file).

resource "aws_iam_role" "responder" {{
  name               = "{role_name}"
  path               = "/service/"
  assume_role_policy = <<-POLICY
{_indent(json.dumps(trust, indent=2), 4)}
  POLICY
}}

resource "aws_iam_role_policy" "responder" {{
  name   = "ops"
  role   = aws_iam_role.responder.id
  policy = <<-POLICY
{_indent(json.dumps(policy, indent=2), 4)}
  POLICY
}}

output "responder_role_arn" {{
  value = aws_iam_role.responder.arn
}}
'''
    return {
        "role_name": role_name,
        "role_arn": f"arn:aws:iam::{account_id}:role/service/{role_name}",
        "module_dir": MODULE_DIR,
        "module_name": MODULE_NAME,
        "files": {f"{MODULE_DIR}/main.tf": main_tf},
        "root_block": (
            f'module "{MODULE_NAME}" {{\n  source = "./{MODULE_DIR}"\n}}\n\n'
            f'output "responder_role_arn" {{\n  value = module.{MODULE_NAME}.responder_role_arn\n}}\n'
        ),
        "policy": policy,
        "trust": trust,
        "enabled_action_types": [r["action_type"] for r in rows if r.get("enabled", True)],
    }


def simulate_actions(iam_client: Any, role_arn: str, policy: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Ask IAM whether the role can do each statement's actions on its
    resources. Used by /check after the role has been applied."""
    results: List[Dict[str, Any]] = []
    for stmt in policy.get("Statement", []):
        actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
        resources = stmt["Resource"] if isinstance(stmt["Resource"], list) else [stmt["Resource"]]
        kwargs: Dict[str, Any] = {"PolicySourceArn": role_arn, "ActionNames": actions}
        if resources != ["*"]:
            kwargs["ResourceArns"] = [r for r in resources if "*" not in r.split(":")[-1].split("/")[0]] or resources
        context = context_entries(stmt)
        if context:
            kwargs["ContextEntries"] = context  # a conditioned statement is implicitDeny without its context
        resp = iam_client.simulate_principal_policy(**kwargs)
        for r in resp.get("EvaluationResults", []):
            results.append({"action": r["EvalActionName"], "resource": r.get("EvalResourceName"), "decision": r["EvalDecision"]})
    return results


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else line for line in text.splitlines())
