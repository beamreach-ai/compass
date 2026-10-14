"""Terraform for the decoy database endpoint (db_credential).

A Fargate service running the listener image behind a Cloud Map name
(`<host_name>.<dns_namespace>`). The listener rejects every login and reports
each attempt to the alert queue through the task role (sqs:SendMessage only).
"""
from __future__ import annotations

from typing import Any, Dict

from captains.honeypot.honeytoken_material import DB_NAMESPACE, DB_USER


def _inner_material(honeytoken: Dict[str, Any]) -> Dict[str, Any]:
    material = honeytoken.get("material") or {}
    return material["material"] if isinstance(material.get("material"), dict) else material


def gen_db_credential(honeytoken: Dict[str, Any]) -> Dict[str, str]:
    from captains.honeypot.terraform_generator import (
        SERVICES_DIR,
        _cover,
        _hcl_string,
        _name_for,
        _tags,
        _variables_tf,
    )

    honeytoken_id = honeytoken["honeytoken_id"]
    cover = _cover(honeytoken)
    material = _inner_material(honeytoken)
    db_user = str(material.get("user") or DB_USER)
    db_password = str(material.get("password") or "")
    token_id = _hcl_string(str(honeytoken_id))
    tags = _tags(honeytoken_id)

    main = f'''# Database endpoint.
#
# A small container service that answers on port 5432 and rejects every login.
# The VPC must be able to reach the container registry, the log service and the
# queue without a NAT gateway: interface endpoints for ecr.api, ecr.dkr, logs
# and sqs, plus an S3 gateway endpoint. This module creates none of them.

locals {{
  cover        = "{cover}"
  host_name    = var.host_name != "" ? var.host_name : local.cover
  queue_parts  = split(":", var.alert_queue_arn)
  queue_region = local.queue_parts[3]
  queue_url    = "https://sqs.${{local.queue_region}}.amazonaws.com/${{local.queue_parts[4]}}/${{local.queue_parts[5]}}"
  cluster_arn  = var.cluster_arn != "" ? var.cluster_arn : aws_ecs_cluster.this[0].arn
  namespace_id = var.namespace_id != "" ? var.namespace_id : aws_service_discovery_private_dns_namespace.this[0].id
}}

variable "vpc_id" {{
  type        = string
  description = "VPC that hosts the service."
}}

variable "subnet_ids" {{
  type        = list(string)
  description = "Private subnets for the service."
}}

variable "image" {{
  type        = string
  description = "Container image URI."
}}

variable "host_name" {{
  type        = string
  default     = ""
  description = "Record name inside the namespace. Defaults to the service name."
}}

variable "dns_namespace" {{
  type        = string
  default     = "{DB_NAMESPACE}"
  description = "Private DNS namespace created when namespace_id is empty."
}}

variable "namespace_id" {{
  type        = string
  default     = ""
  description = "Existing private DNS namespace id. Empty creates one."
}}

variable "cluster_arn" {{
  type        = string
  default     = ""
  description = "Existing cluster ARN. Empty creates one."
}}

variable "cpu" {{
  type    = number
  default = 256
}}

variable "memory" {{
  type    = number
  default = 512
}}

variable "db_user" {{
  type    = string
  default = {_hcl_string(db_user)}
}}

variable "db_password" {{
  type      = string
  default   = {_hcl_string(db_password)}
  sensitive = true
}}

data "aws_vpc" "this" {{
  id = var.vpc_id
}}

resource "aws_ecs_cluster" "this" {{
  count = var.cluster_arn == "" ? 1 : 0
  name  = "{cover}"

{tags}
}}

resource "aws_service_discovery_private_dns_namespace" "this" {{
  count = var.namespace_id == "" ? 1 : 0
  name  = var.dns_namespace
  vpc   = var.vpc_id

{tags}
}}

resource "aws_service_discovery_service" "this" {{
  name = local.host_name

  dns_config {{
    namespace_id   = local.namespace_id
    routing_policy = "MULTIVALUE"

    dns_records {{
      type = "A"
      ttl  = 10
    }}
  }}

{tags}
}}

resource "aws_cloudwatch_log_group" "this" {{
  name              = "/ecs/{cover}"
  retention_in_days = 30

{tags}
}}

data "aws_iam_policy_document" "assume" {{
  statement {{
    actions = ["sts:AssumeRole"]
    principals {{
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }}
  }}
}}

resource "aws_iam_role" "execution" {{
  name               = "{cover}-exec"
  assume_role_policy = data.aws_iam_policy_document.assume.json

{tags}
}}

resource "aws_iam_role_policy_attachment" "execution" {{
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}}

resource "aws_iam_role" "task" {{
  name               = "{cover}-task"
  assume_role_policy = data.aws_iam_policy_document.assume.json

{tags}
}}

resource "aws_iam_role_policy" "task" {{
  name = "send-to-queue"
  role = aws_iam_role.task.id
  policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [{{
      Effect   = "Allow"
      Action   = "sqs:SendMessage"
      Resource = var.alert_queue_arn
    }}]
  }})
}}

resource "aws_security_group" "this" {{
  name        = "{cover}"
  description = "Database port from the VPC"
  vpc_id      = var.vpc_id

  ingress {{
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.this.cidr_block]
  }}

  egress {{
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }}

{tags}
}}

resource "aws_ecs_task_definition" "this" {{
  family                   = "{cover}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{{
    name         = "{cover}"
    image        = var.image
    essential    = true
    portMappings = [{{ containerPort = 5432, protocol = "tcp" }}]
    environment = [
      {{ name = "TOKEN_ID", value = {token_id} }},
      {{ name = "WORKSPACE_ID", value = var.workspace_id }},
      {{ name = "ALERT_QUEUE_URL", value = local.queue_url }},
    ]
    logConfiguration = {{
      logDriver = "awslogs"
      options = {{
        "awslogs-group"         = aws_cloudwatch_log_group.this.name
        "awslogs-region"        = local.queue_region
        "awslogs-stream-prefix" = "db"
      }}
    }}
  }}])

{tags}
}}

resource "aws_ecs_service" "this" {{
  name            = "{cover}"
  cluster         = local.cluster_arn
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {{
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.this.id]
    assign_public_ip = false
  }}

  service_registries {{
    registry_arn = aws_service_discovery_service.this.arn
  }}

{tags}
}}

output "host" {{
  value = "${{local.host_name}}.${{var.dns_namespace}}"
}}

output "user" {{
  value = var.db_user
}}

output "password" {{
  value     = var.db_password
  sensitive = true
}}
'''
    slug = _name_for(honeytoken)
    return {
        f"{SERVICES_DIR}/{slug}/main.tf": main,
        f"{SERVICES_DIR}/{slug}/variables.tf": _variables_tf(),
    }
