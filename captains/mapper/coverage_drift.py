from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Tuple

from infra_map.models import RelationshipEdge, ResourceNode

logger = logging.getLogger("coverage-drift")

# Maps (tf_resource_type, tf_attribute_name) -> aws_attribute_name in ResourceNode.attributes.
# tf attribute names are snake_case from the state file; aws attribute names are camelCase
# as stored by AWSGraphCollector.
_DRIFT_FIELDS: Dict[str, List[Tuple[str, str]]] = {
    "aws_lambda_function": [
        ("memory_size", "memorySize"),
        ("timeout", "timeout"),
        ("runtime", "runtime"),
    ],
    "aws_instance": [
        ("instance_type", "instanceType"),
    ],
    "aws_ecs_service": [
        ("desired_count", "desiredCount"),
    ],
    "aws_ecs_cluster": [],
    "aws_dynamodb_table": [
        ("billing_mode", "billingMode"),
    ],
    "aws_sqs_queue": [
        ("visibility_timeout_seconds", "visibilityTimeout"),
    ],
    "aws_cloudwatch_log_group": [
        ("retention_in_days", "retentionInDays"),
    ],
    "aws_lb": [
        ("internal", "scheme"),
    ],
    "aws_alb": [
        ("internal", "scheme"),
    ],
    "aws_secretsmanager_secret": [
        ("kms_key_id", "kmsKeyId"),
    ],
    "aws_iam_role": [
        ("max_session_duration", "maxSessionDuration"),
    ],
    "aws_sns_topic": [],
    "aws_sfn_state_machine": [
        ("type", "type"),
    ],
}


def _to_dynamo_safe(value: Any) -> Any:
    """Convert float to Decimal so DynamoDB can serialise it."""
    if isinstance(value, float):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return str(value)
    return value


def _coerce(value: Any) -> Any:
    """Normalise values before comparison to reduce noise from type differences."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return value


def annotate_coverage(
    nodes: List[ResourceNode],
    edges: List[RelationshipEdge],
) -> None:
    """
    Set coverage_score (1.0 = managed, 0.0 = unmanaged) on every aws_scan node.
    Relies on the 'observes' edges written by TerraformStateGraphCollector.
    """
    observed_ids = {
        edge.to_resource_id
        for edge in edges
        if edge.relationship_type == "observes"
    }
    for node in nodes:
        if node.source != "aws_scan":
            continue
        node.attributes["coverage_score"] = 1 if node.resource_id in observed_ids else 0


def annotate_drift(
    nodes: List[ResourceNode],
    edges: List[RelationshipEdge],
) -> None:
    """
    For each aws_scan node that is observed by a tf_state_instance, compare
    the declared state attributes against the live AWS attributes and write:
      - drift_status: "in_sync" | "drifted" | "no_data"
      - drift_fields: list of {field, declared, actual} dicts for drifted keys
    """
    node_by_id = {node.resource_id: node for node in nodes}

    # observes: tf_state_instance -> aws_resource
    for edge in edges:
        if edge.relationship_type != "observes":
            continue

        instance = node_by_id.get(edge.from_resource_id)
        aws_node = node_by_id.get(edge.to_resource_id)
        if not instance or not aws_node:
            continue
        if aws_node.source != "aws_scan":
            continue

        tf_resource_type = (instance.attributes or {}).get("resource_type")
        state_attrs = (instance.attributes or {}).get("state_attributes")

        if not tf_resource_type or not isinstance(state_attrs, dict) or not state_attrs:
            aws_node.attributes.setdefault("drift_status", "no_data")
            continue

        field_pairs = _DRIFT_FIELDS.get(str(tf_resource_type), [])
        if not field_pairs:
            # Known type but no driftable fields defined — mark in_sync
            aws_node.attributes.setdefault("drift_status", "in_sync")
            aws_node.attributes.setdefault("drift_fields", [])
            continue

        drifted: List[Dict[str, Any]] = []
        for tf_key, aws_key in field_pairs:
            declared = state_attrs.get(tf_key)
            actual = (aws_node.attributes or {}).get(aws_key)
            if declared is None or actual is None:
                continue
            if _coerce(declared) != _coerce(actual):
                drifted.append({
                    "field": aws_key,
                    "declared": _to_dynamo_safe(declared),
                    "actual": _to_dynamo_safe(actual),
                })

        if drifted:
            aws_node.attributes["drift_status"] = "drifted"
            aws_node.attributes["drift_fields"] = drifted
        else:
            aws_node.attributes.setdefault("drift_status", "in_sync")
            aws_node.attributes.setdefault("drift_fields", [])
