from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from infra_map.facts import EdgeFact, FactBatch, NodeFact
from infra_map.materializer import InfraMapMaterializer
from infra_map.models import RelationshipEdge, ResourceNode

from .infra_map_ids import (
    state_file_scope,
    tf_state_file_node_id,
    tf_state_instance_node_id,
)

LOGGER = logging.getLogger("tf-state-graph")


def _dynamo_safe(value: Any) -> Any:
    """Recursively convert float → Decimal so DynamoDB can serialise the value."""
    if isinstance(value, float):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return str(value)
    if isinstance(value, dict):
        return {k: _dynamo_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dynamo_safe(v) for v in value]
    return value


def _parse_arn(arn: str) -> Dict[str, str]:
    parts = str(arn).split(":")
    if len(parts) < 6:
        return {}
    return {
        "partition": parts[1],
        "service": parts[2],
        "region": parts[3],
        "account": parts[4],
        "resource": ":".join(parts[5:]),
    }


def _state_files_from_buckets(s3_client, state_buckets: Sequence[dict]) -> Iterable[dict]:
    for bucket_config in state_buckets or []:
        bucket = bucket_config.get("bucket")
        if not bucket:
            continue
        prefix = bucket_config.get("prefix", "")
        paginator = s3_client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj.get("Key")
                    if not key or not str(key).endswith((".tfstate", ".tfstate.backup")):
                        continue
                    yield {
                        "bucket": bucket,
                        "key": key,
                        "last_modified": obj.get("LastModified"),
                    }
        except (BotoCoreError, ClientError) as exc:
            LOGGER.warning("⚠️ Could not scan bucket %s for Terraform state: %s", bucket, exc)


class TerraformStateGraphCollector:
    def __init__(
        self,
        org_id: str,
        target_session: boto3.Session,
        *,
        state_files: Optional[Sequence[dict]] = None,
        state_buckets: Optional[Sequence[dict]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.org_id = org_id
        self.target_session = target_session
        self.state_files = list(state_files or [])
        self.state_buckets = list(state_buckets or [])
        self.logger = logger or LOGGER
        self.materializer = InfraMapMaterializer()

    def collect(
        self,
        *,
        existing_nodes: Sequence[ResourceNode] = (),
    ) -> Tuple[List[ResourceNode], List[RelationshipEdge]]:
        node_map: Dict[str, ResourceNode] = {}
        edge_map: Dict[Tuple[str, str, str], RelationshipEdge] = {}
        for batch in self.collect_fact_batches(existing_nodes=existing_nodes):
            nodes, edges = self.materializer.materialize_batch(batch)
            for node in nodes:
                node_map[node.resource_id] = node
            for edge in edges:
                edge_map[(edge.from_resource_id, edge.to_resource_id, edge.relationship_type)] = edge
        return list(node_map.values()), list(edge_map.values())

    def collect_fact_batches(
        self,
        *,
        existing_nodes: Sequence[ResourceNode] = (),
    ) -> List[FactBatch]:
        state_files = self._resolve_state_files()
        if not state_files:
            return []

        existing_ids = {node.resource_id for node in existing_nodes}
        s3 = self.target_session.client("s3")
        batches: List[FactBatch] = []
        for state_file in state_files:
            batch = self._process_state_file(s3, state_file, existing_ids)
            if batch is not None:
                batches.append(batch)
        return batches

    def _resolve_state_files(self) -> List[dict]:
        discovered: Dict[Tuple[str, str], dict] = {}

        for state_file in self.state_files:
            bucket = state_file.get("bucket")
            key = state_file.get("key")
            if isinstance(bucket, str) and isinstance(key, str) and bucket and key:
                discovered[(bucket, key)] = {
                    "bucket": bucket,
                    "key": key,
                    **({"last_modified": state_file["last_modified"]} if state_file.get("last_modified") else {}),
                }

        if not discovered and self.state_buckets:
            s3 = self.target_session.client("s3")
            for state_file in _state_files_from_buckets(s3, self.state_buckets):
                discovered[(state_file["bucket"], state_file["key"])] = state_file

        return [discovered[key] for key in sorted(discovered.keys())]

    def _process_state_file(self, s3_client, state_file: dict, existing_ids: set[str]) -> Optional[FactBatch]:
        bucket = state_file["bucket"]
        key = state_file["key"]
        tf_state_file_id = tf_state_file_node_id(bucket, key)
        scope = state_file_scope(tf_state_file_id)
        payload = self._load_state_payload(s3_client, bucket, key)
        if not payload:
            return None

        last_modified = state_file.get("last_modified")
        node_facts: Dict[str, NodeFact] = {}
        edge_facts: Dict[Tuple[str, str, str], EdgeFact] = {}

        file_node = NodeFact(
            org_id=self.org_id,
            entity_id=tf_state_file_id,
            node_type="tf_state_file",
            account_id="",
            region="global",
            name=key.rsplit("/", 1)[-1],
            node_class="artifact",
            attributes={
                "bucket": bucket,
                "key": key,
                "backend": "s3",
                "s3_uri": f"s3://{bucket}/{key}",
                **({"last_modified": last_modified.isoformat()} if last_modified else {}),
            },
            evidence=["terraform_state"],
        )
        node_facts[file_node.entity_id] = file_node

        for resource in payload.get("resources", []):
            resource_type = resource.get("type")
            resource_name = resource.get("name")
            module_path = resource.get("module")
            mode = resource.get("mode")
            for instance in resource.get("instances", []):
                values = instance.get("attributes") or instance.get("values") or {}
                aws_resource_id = values.get("arn") or values.get("id")
                if not aws_resource_id:
                    continue
                instance_key = instance.get("index_key")
                address_parts = [module_path, f"{resource_type}.{resource_name}"]
                resource_address = ".".join(part for part in address_parts if part)
                instance_id = tf_state_instance_node_id(
                    tf_state_file_id,
                    resource_address,
                    instance_key=instance_key,
                    aws_resource_id=aws_resource_id,
                )
                instance_node = NodeFact(
                    org_id=self.org_id,
                    entity_id=instance_id,
                    node_type="tf_state_instance",
                    account_id="",
                    region="global",
                    name=resource_address,
                    node_class="artifact",
                    attributes={
                        "resource_type": resource_type,
                        "resource_name": resource_name,
                        "resource_address": resource_address,
                        "raw_address": resource_address,
                        "module_path": module_path,
                        "mode": mode,
                        "instance_key": instance_key,
                        "aws_resource_id": aws_resource_id,
                        "state_file_id": tf_state_file_id,
                        "state_attributes": _dynamo_safe(values),
                    },
                    evidence=["terraform_state"],
                )
                node_facts[instance_node.entity_id] = instance_node
                contains_edge = EdgeFact(
                    org_id=self.org_id,
                    from_entity_id=tf_state_file_id,
                    to_entity_id=instance_id,
                    edge_type="contains",
                    confidence="high",
                    evidence=["terraform_state"],
                )
                edge_facts[(contains_edge.from_entity_id, contains_edge.to_entity_id, contains_edge.edge_type)] = contains_edge
                if aws_resource_id not in existing_ids and aws_resource_id not in node_facts:
                    inferred = self._inferred_resource_node(aws_resource_id, resource_type)
                    node_facts[inferred.entity_id] = inferred
                observes_edge = EdgeFact(
                    org_id=self.org_id,
                    from_entity_id=instance_id,
                    to_entity_id=aws_resource_id,
                    edge_type="observes",
                    confidence="high",
                    evidence=["terraform_state"],
                    attributes={
                        "resource_address": resource_address,
                        "module_path": module_path,
                    },
                )
                edge_facts[(observes_edge.from_entity_id, observes_edge.to_entity_id, observes_edge.edge_type)] = observes_edge

        return FactBatch(
            org_id=self.org_id,
            source="tf_state",
            scope=scope,
            node_facts=list(node_facts.values()),
            edge_facts=list(edge_facts.values()),
        )

    def _load_state_payload(self, s3_client, bucket: str, key: str) -> Optional[dict]:
        try:
            obj = s3_client.get_object(Bucket=bucket, Key=key)
            payload = obj["Body"].read().decode("utf-8")
            parsed = json.loads(payload)
        except (BotoCoreError, ClientError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.logger.warning("⚠️ Could not parse Terraform state %s/%s: %s", bucket, key, exc)
            return None
        return parsed if isinstance(parsed, dict) else None

    def _inferred_resource_node(
        self,
        resource_id: str,
        resource_type: Optional[str],
    ) -> NodeFact:
        arn = _parse_arn(resource_id)
        return NodeFact(
            org_id=self.org_id,
            entity_id=resource_id,
            node_type="aws_resource",
            account_id=arn.get("account", ""),
            region=arn.get("region", ""),
            name=resource_id.split("/")[-1].split(":")[-1],
            node_class="resource",
            attributes={
                "source": "tf_state",
                "terraform_resource_type": resource_type,
                "inferred": True,
            },
            confidence="medium",
            evidence=["terraform_state:inferred_resource"],
        )
