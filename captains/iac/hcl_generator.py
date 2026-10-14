from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from compass_core.llm import model_for
from infra_map.models import ResourceNode

logger = logging.getLogger("iac-hcl-generator")

_AWS_TYPE_TO_TF: Dict[str, str] = {
    "lambda_function": "aws_lambda_function",
    "ec2_instance": "aws_instance",
    "ec2_security_group": "aws_security_group",
    "ecs_service": "aws_ecs_service",
    "ecs_cluster": "aws_ecs_cluster",
    "ecs_task_definition": "aws_ecs_task_definition",
    "elbv2_load_balancer": "aws_lb",
    "elbv2_target_group": "aws_lb_target_group",
    "dynamodb_table": "aws_dynamodb_table",
    "sqs_queue": "aws_sqs_queue",
    "sns_topic": "aws_sns_topic",
    "secretsmanager_secret": "aws_secretsmanager_secret",
    "iam_role": "aws_iam_role",
    "cloudwatch_log_group": "aws_cloudwatch_log_group",
    "cloudwatch_alarm": "aws_cloudwatch_metric_alarm",
    "eventbridge_rule": "aws_cloudwatch_event_rule",
    "stepfunctions_state_machine": "aws_sfn_state_machine",
    "s3_bucket": "aws_s3_bucket",
    "rds_instance": "aws_db_instance",
    "rds_cluster": "aws_rds_cluster",
}


# AWS types the mapper discovers that are not standalone importable Terraform
# resources. A DynamoDB stream, for instance, has no `aws_dynamodb_stream`
# resource at all — it is configured through `stream_enabled` on the table it
# belongs to. Naming them explicitly is what lets them be reported as skipped
# instead of silently turned into HCL that cannot plan.
_NOT_IMPORTABLE: Dict[str, str] = {
    "dynamodb_stream": (
        "DynamoDB streams have no standalone Terraform resource; they are configured "
        "via stream_enabled/stream_view_type on the aws_dynamodb_table that owns them"
    ),
    "ec2_network_interface": (
        "ENIs are almost always created implicitly by the resource they attach to "
        "(instance, NAT gateway, load balancer) and should not be imported directly"
    ),
}


def _tf_name(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]", "_", str(name)).strip("_").lower()
    if s and s[0].isdigit():
        s = "r_" + s
    return s or "resource"


def _log_group_name(resource_id: str) -> str:
    """`/aws/lambda/foo` out of `arn:...:log-group:/aws/lambda/foo:*`."""
    if not resource_id.startswith("arn:"):
        return resource_id
    _, _, tail = resource_id.partition(":log-group:")
    return re.sub(r":\*$", "", tail) or resource_id


def _after_last(sep: str):
    return lambda resource_id: resource_id.rsplit(sep, 1)[-1] if sep in resource_id else resource_id


# Terraform's import ID is per-resource-type and is frequently *not* the ARN.
# Passing the mapper's resource_id through unchanged produced import blocks that
# fail immediately — a log group wants its name, a DynamoDB table wants its
# table name. Types whose import ID genuinely is the ARN (aws_lb,
# aws_sfn_state_machine, aws_secretsmanager_secret, …) are absent on purpose and
# fall through unchanged.
_IMPORT_ID_EXTRACTORS: Dict[str, Any] = {
    "aws_cloudwatch_log_group": _log_group_name,
    "aws_dynamodb_table": _after_last("table/"),
    "aws_iam_role": _after_last("role/"),
    "aws_s3_bucket": _after_last(":::"),
    "aws_cloudwatch_metric_alarm": _after_last(":alarm:"),
    "aws_cloudwatch_event_rule": _after_last(":rule/"),
    "aws_ecs_cluster": _after_last(":cluster/"),
    "aws_lambda_function": _after_last(":function:"),
    "aws_db_instance": _after_last(":db:"),
    "aws_rds_cluster": _after_last(":cluster:"),
}


def _import_id(tf_type: str, resource_id: str) -> str:
    extractor = _IMPORT_ID_EXTRACTORS.get(tf_type)
    return extractor(resource_id) if extractor else resource_id


# Generated code goes into dedicated files rather than into main.tf. Terraform
# loads every .tf in a directory, so a new file is equivalent to appending —
# without ever reading, rewriting, or risking the hand-maintained config that
# already lives there.
_IMPORTS_FILENAME = "beamreach_imports.tf"
_RESOURCES_FILENAME = "beamreach_imported.tf"


def _dest_dir(module_path: Optional[str]) -> str:
    """Normalise the module path into a directory prefix for generated files.

    `module_path` is a *filesystem* location — where to place the new code —
    and nothing more. It is deliberately not translated into a Terraform
    module address: a directory path cannot tell you one. `environments/demo`
    holding its own backend and provider is a root module whose resources are
    addressed with no prefix at all, while a genuine child module is addressed
    by the name its parent calls it with, which does not have to match the
    directory name.
    """
    if not module_path:
        return ""
    parts = [p for p in module_path.replace("\\", "/").split("/") if p and p != "."]
    return "/".join(parts)


class HCLGenerator:
    def __init__(self, target: str, config: Dict[str, Any]):
        self.target = target
        self.config = config
        self._bedrock = model_for(config, target=target)

    def _model(self):
        if self._bedrock is None:
            raise RuntimeError("no language model is configured for HCL generation (compass_core.llm.set_model_factory)")
        return self._bedrock

    def generate(
        self,
        resources: List[ResourceNode],
        *,
        module_path: Optional[str] = None,
        is_new_module: bool = False,
    ) -> Dict[str, str]:
        files: Dict[str, str] = {}
        dest_dir = _dest_dir(module_path)
        prefix = f"{dest_dir}/" if dest_dir else ""

        importable, skipped = self.partition_importable(resources)
        if skipped:
            logger.warning(
                "Skipping %d resource(s) with no standalone Terraform resource: %s",
                len(skipped),
                ", ".join(f"{n.name} ({n.type})" for n, _ in skipped),
            )
        if not importable:
            raise RuntimeError(
                "None of the selected resources can be imported into Terraform: "
                + "; ".join(f"{n.name} ({n.type}) — {why}" for n, why in skipped)
            )

        # Both files land in the same directory. Terraform only accepts import
        # blocks in a root module, and an import block's address is resolved
        # relative to the module it is declared in — so colocating them with
        # the resource blocks they refer to is what makes unprefixed addresses
        # correct.
        files[f"{prefix}{_IMPORTS_FILENAME}"] = self._generate_import_blocks(importable)

        resource_hcl = self._generate_resource_hcl_with_claude(
            resources=importable,
            module_path=module_path,
            is_new_module=is_new_module,
        )
        header = [
            f"# Resource blocks for {len(importable)} resource(s) imported by Beamreach Compass.",
            f"# Paired with the import blocks in {_IMPORTS_FILENAME}.",
            "# Review the attributes below against the live resources before applying.",
        ]
        if skipped:
            header.append("#")
            header.append("# Not imported:")
            header += [f"#   - {n.name} ({n.type}): {why}" for n, why in skipped]
        files[f"{prefix}{_RESOURCES_FILENAME}"] = "\n".join(header) + "\n\n" + resource_hcl.lstrip("\n")

        return files

    def partition_importable(
        self,
        resources: List[ResourceNode],
    ) -> tuple[List[ResourceNode], List[tuple[ResourceNode, str]]]:
        """Split resources into those Terraform can import and those it cannot.

        Anything whose AWS type has no known Terraform resource is separated out
        rather than guessed at. The previous fallback invented a type name from
        the AWS one (`dynamodb_stream` -> `aws_dynamodb_stream`), producing HCL
        that refers to resources no provider defines.
        """
        importable: List[ResourceNode] = []
        skipped: List[tuple[ResourceNode, str]] = []
        for node in resources:
            aws_type = node.type or ""
            if aws_type in _NOT_IMPORTABLE:
                skipped.append((node, _NOT_IMPORTABLE[aws_type]))
            elif aws_type not in _AWS_TYPE_TO_TF:
                skipped.append((node, f"no Terraform resource type is mapped for AWS type '{aws_type}'"))
            else:
                importable.append(node)
        return importable, skipped

    def _tf_resource_type(self, node: ResourceNode) -> str:
        tf_type = _AWS_TYPE_TO_TF.get(node.type or "")
        if not tf_type:
            raise KeyError(
                f"No Terraform resource type mapped for AWS type '{node.type}' "
                "(call partition_importable first)"
            )
        return tf_type

    def resource_addresses(self, resources: List[ResourceNode]) -> List[str]:
        """Terraform addresses the import blocks will target."""
        return [f"{self._tf_resource_type(n)}.{_tf_name(n.name or n.resource_id)}" for n in resources]

    def import_ids(self, resources: List[ResourceNode]) -> List[str]:
        """The per-type import IDs, which rewiring must never substitute away."""
        return [_import_id(self._tf_resource_type(n), n.resource_id) for n in resources]

    def generate_import_blocks(self, resources: List[ResourceNode]) -> str:
        """Public entry point for the terraform-backed pipeline, which needs the
        import blocks on their own to drive config generation."""
        return self._generate_import_blocks(resources)

    def _generate_import_blocks(self, resources: List[ResourceNode]) -> str:
        lines: List[str] = [
            "# Terraform import blocks (requires Terraform >= 1.5)",
            f"# Resource blocks for these imports are in {_RESOURCES_FILENAME}.",
            "# Once `terraform apply` has completed the import, this file can be deleted;",
            f"# keep {_RESOURCES_FILENAME}.",
            "",
        ]
        for node in resources:
            tf_type = self._tf_resource_type(node)
            raw_name = node.name or node.resource_id.split("/")[-1].split(":")[-1]
            name = _tf_name(raw_name)
            # Addresses are unprefixed: these blocks sit in the same module as
            # the resource blocks they target.
            lines.append("import {")
            lines.append(f"  to = {tf_type}.{name}")
            lines.append(f'  id = "{_import_id(tf_type, node.resource_id)}"')
            lines.append("}")
            lines.append("")
        return "\n".join(lines)

    def _resource_summary(self, node: ResourceNode) -> Dict[str, Any]:
        attrs = node.attributes or {}
        # Limit attributes to avoid huge payloads
        trimmed_attrs = dict(list(attrs.items())[:25])
        return {
            "resource_id": node.resource_id,
            "aws_type": node.type,
            "tf_resource_type": self._tf_resource_type(node),
            "tf_name": _tf_name(node.name or node.resource_id.split("/")[-1]),
            "name": node.name,
            "region": node.region,
            "account_id": node.account_id,
            "attributes": trimmed_attrs,
        }

    def _generate_resource_hcl_with_claude(
        self,
        resources: List[ResourceNode],
        module_path: Optional[str],
        is_new_module: bool,
    ) -> str:
        resource_data = json.dumps(
            [self._resource_summary(r) for r in resources],
            indent=2,
            default=str,
        )
        context = f"Target path: {module_path or '(root module)'}\nNew module: {is_new_module}"

        prompt = f"""Generate Terraform HCL resource blocks for these AWS resources.

{context}

Resources:
{resource_data}

Rules:
1. Write one resource block per resource using the provided tf_resource_type and tf_name
2. Populate attributes ONLY from the live AWS data in the attributes dict
3. Omit read-only/computed fields (id, arn, tags_all, etc.) from the body
4. Use the region from the resource data for any provider aliases if needed
5. Add a one-line comment above each block with the resource name
6. Output ONLY valid HCL — no markdown, no explanations, no code fences

Accuracy rules — these blocks are paired with import blocks, so a value that
does not match the live resource makes Terraform plan a change or a
replacement against real infrastructure:
7. NEVER invent, guess or use placeholder values. Do not write filler like
   "Managed by Terraform" for a description.
8. If a required argument is not present in the attributes dict, emit it as
   `# TODO: <argument> — not present in scanned attributes` on its own line
   instead of inventing a value. A visible gap is safe; a wrong value is not.
9. Reproduce immutable arguments exactly as scanned. For aws_security_group
   that means `name` and `description` verbatim — AWS cannot change a security
   group description in place, so a mismatch forces a destroy-and-recreate.
10. Include nested blocks the live data provides (for example security group
    ingress/egress rules). Omitting them makes the first apply after import
    look like a request to delete every rule."""

        system_prompt = (
            "You are a Terraform expert. Generate valid HCL resource configurations "
            "from live AWS resource data, for resources that are about to be imported "
            "into state. Fidelity to the scanned data matters more than completeness: "
            "a guessed value causes Terraform to modify or destroy real infrastructure, "
            "while a missing one only causes a plan error. Never invent values. "
            "Output only HCL code without markdown fences."
        )
        result = self._model().call(
            messages=[{"role": "user", "content": prompt}],
            system_prompt=system_prompt,
            max_tokens=4000,
            temperature=0.1,
            timeout=120,
        )

        result = re.sub(r"^```(?:hcl|terraform)?\s*\n?", "", result.strip(), flags=re.IGNORECASE)
        result = re.sub(r"\n?```\s*$", "", result.strip())
        return result

    def generate_drift_fix(self, drift_data: List[Dict[str, Any]]) -> str:
        data_str = json.dumps(drift_data, indent=2, default=str)
        prompt = f"""Generate Terraform HCL resource blocks that correct configuration drift.

For each resource use the ACTUAL AWS values (not the declared values) to produce the correct resource block.

Drift data:
{data_str}

Rules:
1. Write one resource block per resource using the tf type derived from resource_type and the name field
2. For each drifted field, add a comment: # drift: declared=<value> actual=<value>
3. Use the 'actual' value in the resource body
4. Omit read-only computed attributes (id, arn, tags_all, owner_id, etc.)
5. If resource_address is provided, use it to derive the Terraform address
6. Output ONLY valid HCL — no markdown, no explanations"""

        system_prompt = (
            "You are a Terraform expert. Generate HCL to correct configuration drift. "
            "Output only HCL code without markdown fences."
        )
        result = self._model().call(
            messages=[{"role": "user", "content": prompt}],
            system_prompt=system_prompt,
            max_tokens=4000,
            temperature=0.1,
            timeout=120,
        )
        result = re.sub(r"^```(?:hcl|terraform)?\s*\n?", "", result.strip(), flags=re.IGNORECASE)
        result = re.sub(r"\n?```\s*$", "", result.strip())
        return result
