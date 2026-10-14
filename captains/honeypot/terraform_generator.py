"""Generates Terraform HCL for each honeytoken class.

Design notes:
- Everything the customer's AWS account (and `terraform plan`) can show derives
  from the honeytoken's cover name alone: resource names, rule names, target ids,
  Lambda names, object keys, DNS names, log groups, resource labels, module
  addresses and input names. No product words appear in generated text.
- The only link back to Compass is one neutral tag, `cost-center = <8 hex>`,
  derived from sha256(honeytoken_id) (`stealth_tag`). Compass keeps the real
  mapping in DynamoDB.
- Every per-token directory ships a `variables.tf` so the queue ARN / workspace /
  callback references resolve.
- The EventBridge `input_template` bakes the workspace id and token id as
  literals, because heredoc template content is passed to EventBridge verbatim
  and Terraform does NOT interpolate `var.*` inside it.
- All EventBridge-based generators emit one payload shape (`_event_target`).
  Paths absent from an event render as empty strings, so ingestion treats ""
  as missing.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Tuple

from captains.honeypot.classes import normalize_class
from captains.honeypot.generators.db_credential import gen_db_credential
from captains.honeypot.honeytoken_material import (
    cover_label,
    default_cover_name,
    s3_object_content,
)

SERVICES_DIR = "services"
_PRODUCT_WORDS = re.compile("honeypot|honeytoken|cana" + "ry|beamreach|compass|hp[-_]", re.IGNORECASE)


def stealth_tag(honeytoken_id: str) -> Tuple[str, str]:
    """The one neutral tag on every taggable resource: (key, value).

    The value is opaque (8 hex of sha256(honeytoken_id)); /arm finds a resource
    by it without the tag revealing anything."""
    return "cost-center", hashlib.sha256(str(honeytoken_id).encode()).hexdigest()[:8]


def _tags(honeytoken_id: str) -> str:
    key, value = stealth_tag(honeytoken_id)
    return f'  tags = {{\n    "{key}" = "{value}"\n  }}'


def _safe_name(name: str, seed: str = "") -> str:
    if _PRODUCT_WORDS.search(name or ""):
        name = default_cover_name(seed)
    return re.sub(r"[^a-z0-9_]", "_", name.lower())[:48].strip("_") or re.sub(
        r"[^a-z0-9_]", "_", default_cover_name(seed)
    )


def _name_for(honeytoken: Dict[str, Any]) -> str:
    """Underscore slug of the cover name (directory / module label)."""
    return _safe_name(
        str(honeytoken.get("name") or default_cover_name(str(honeytoken.get("honeytoken_id", "")))),
        str(honeytoken.get("honeytoken_id", "")),
    )


def _cover(honeytoken: Dict[str, Any]) -> str:
    """Hyphenated cover name used in every AWS-visible name."""
    return _name_for(honeytoken).replace("_", "-")


def _hcl_string(value: str) -> str:
    """A quoted HCL string literal with template sequences escaped."""
    return json.dumps(value).replace("${", "$${").replace("%{", "%%{")


def _variables_tf() -> str:
    """Input variables of every per-token module.

    The root (`services.tf`, written by the PR writer) wires them; the module
    declares no providers, so it inherits the root's provider configuration."""
    return '''variable "alert_queue_arn" {
  type        = string
  description = "ARN of the queue that receives event notifications."
}

variable "alert_delivery_role_arn" {
  type        = string
  description = "IAM role EventBridge assumes to send to the queue."
}

variable "workspace_id" {
  type        = string
  description = "Identifier forwarded with every event."
}

variable "callback_url" {
  type        = string
  description = "Endpoint that receives callback events."
  default     = ""
}
'''


ACCOUNT_MODULE_DIR = f"{SERVICES_DIR}/_account"
ACCOUNT_MODULE_NAME = "svc_account"
ROOT_FILE = "services.tf"
TOKEN_MODULE_PREFIX = "svc_"


def account_module_files() -> Dict[str, str]:
    """Once-per-account module: the EventBridge delivery role every rule target uses."""
    main = '''# Event forwarding role.
# EventBridge needs a role in this account to put events on a cross-account queue.

resource "aws_iam_role" "alert_delivery" {
  name = "svc-event-forwarder"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "alert_delivery" {
  name = "send-to-queue"
  role = aws_iam_role.alert_delivery.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "sqs:SendMessage"
      Resource = var.alert_queue_arn
    }]
  })
}

output "alert_delivery_role_arn" {
  value = aws_iam_role.alert_delivery.arn
}
'''
    variables = '''variable "alert_queue_arn" {
  type        = string
  description = "ARN of the queue that receives event notifications."
}
'''
    return {
        f"{ACCOUNT_MODULE_DIR}/main.tf": main,
        f"{ACCOUNT_MODULE_DIR}/variables.tf": variables,
    }


def module_slug(honeytoken: Dict[str, Any]) -> str:
    """Directory / module-name slug of this token's generated module."""
    return _name_for(honeytoken)


def root_outputs(honeytoken: Dict[str, Any]) -> Dict[str, str]:
    """Outputs the root re-exports for this token: {module output name: root-output suffix}.

    Only what the control plane needs. Secrets (secret key, private key) are never re-exported."""
    return {
        "aws_credential": {"access_key_id": "access_key_id"},
        "api_token": {"callback_url": "callback_url"},
    }.get(normalize_class(honeytoken.get("honeytoken_class", "")), {})


def generate_terraform(honeytoken: Dict[str, Any]) -> Dict[str, str]:
    """Return a dict of {file_path: hcl_content} for this honeytoken."""
    honeytoken_class = normalize_class(honeytoken.get("honeytoken_class", ""))
    generators = {
        "s3_object": _gen_s3_object,
        "aws_credential": _gen_aws_credential,
        "api_token": _gen_api_token,
        "dns_record": _gen_dns_record,
        "ssh_key": _gen_ssh_key,
        "db_credential": gen_db_credential,
    }
    generator = generators.get(honeytoken_class)
    if not generator:
        raise ValueError(f"Unknown honeytoken class: {honeytoken_class}")
    files = generator(honeytoken)
    name = _name_for(honeytoken)
    files[f"{SERVICES_DIR}/{name}/variables.tf"] = _variables_tf()
    return files


def _workspace_literal(honeytoken: Dict[str, Any]) -> str:
    """Workspace id baked as a literal into EventBridge templates."""
    return str(honeytoken.get("workspace_id") or "").replace('"', "")


def _event_target(honeytoken_id: str, workspace_id: str, rule_ref: str, service: str) -> str:
    """The EventBridge target block shared by every rule-based class.

    One payload shape for all of them; `token_id` is the honeytoken id."""
    return f'''resource "aws_cloudwatch_event_target" "target" {{
  rule      = {rule_ref}.name
  target_id = local.cover
  arn       = var.alert_queue_arn
  role_arn  = var.alert_delivery_role_arn

  input_transformer {{
    input_paths = {{
      "eventId"       = "$.detail.eventID"
      "eventTime"     = "$.detail.eventTime"
      "eventName"     = "$.detail.eventName"
      "region"        = "$.detail.awsRegion"
      "accessKeyId"   = "$.detail.userIdentity.accessKeyId"
      "principalArn"  = "$.detail.userIdentity.arn"
      "sourceIp"      = "$.detail.sourceIPAddress"
      "userAgent"     = "$.detail.userAgent"
      "errorCode"     = "$.detail.errorCode"
      "vpcEndpointId" = "$.detail.vpcEndpointId"
    }}
    input_template = <<TEMPLATE
{{
  "token_id": "{honeytoken_id}",
  "workspace_id": "{workspace_id}",
  "event_id": "<eventId>",
  "event_time": "<eventTime>",
  "event_name": "<eventName>",
  "region": "<region>",
  "access_key_id": "<accessKeyId>",
  "principal_arn": "<principalArn>",
  "source_ip": "<sourceIp>",
  "user_agent": "<userAgent>",
  "error_code": "<errorCode>",
  "vpc_endpoint_id": "<vpcEndpointId>",
  "service": "{service}"
}}
TEMPLATE
  }}
}}
'''


# ── S3 object ───────────────────────────────────────────────────────

def _gen_s3_object(honeytoken: Dict[str, Any]) -> Dict[str, str]:
    honeytoken_id = honeytoken["honeytoken_id"]
    name = _name_for(honeytoken)
    cover = _cover(honeytoken)
    workspace_id = _workspace_literal(honeytoken)
    resource_path = honeytoken.get("resource_path", "")
    bucket = resource_path.split("/")[0] if "/" in resource_path else resource_path
    object_key = "/".join(resource_path.split("/")[1:]) if "/" in resource_path else f"secrets/{cover}.env"
    content = _s3_content(honeytoken)

    hcl = f'''locals {{
  cover = "{cover}"
}}

resource "aws_s3_object" "this" {{
  bucket  = "{bucket}"
  key     = "{object_key}"
  content = {_hcl_string(content)}

{_tags(honeytoken_id)}
}}

# S3 object reads (GetObject) are data-plane events. They only reach
# CloudTrail/EventBridge if data-event logging is enabled for this bucket. The
# trail below enables it for exactly this object; set the variable to false if
# an existing trail already covers it.
variable "enable_data_trail" {{
  type    = bool
  default = true
}}

resource "aws_cloudtrail" "data" {{
  count                         = var.enable_data_trail ? 1 : 0
  name                          = "{cover}-data"
  s3_bucket_name                = "{bucket}"
  include_global_service_events = false
  is_multi_region_trail         = false
  enable_logging                = true

  event_selector {{
    read_write_type           = "ReadOnly"
    include_management_events = false
    data_resource {{
      type   = "AWS::S3::Object"
      values = ["arn:aws:s3:::{bucket}/{object_key}"]
    }}
  }}

{_tags(honeytoken_id)}
}}

resource "aws_cloudwatch_event_rule" "rule" {{
  name        = "{cover}-activity"
  description = "Object access events"

  event_pattern = jsonencode({{
    source      = ["aws.s3"]
    detail-type = ["AWS API Call via CloudTrail"]
    detail = {{
      eventSource = ["s3.amazonaws.com"]
      eventName   = ["GetObject", "HeadObject"]
      requestParameters = {{
        bucketName = ["{bucket}"]
        key        = ["{object_key}"]
      }}
    }}
  }})
}}

{_event_target(honeytoken_id, workspace_id, "aws_cloudwatch_event_rule.rule", "s3")}'''
    return {f"{SERVICES_DIR}/{name}/main.tf": hcl}


def _s3_content(honeytoken: Dict[str, Any]) -> str:
    """The object body: exactly what the material preview shows, when the token carries it."""
    material = honeytoken.get("material") or {}
    inner = material.get("material") if isinstance(material.get("material"), dict) else material
    content = (inner or {}).get("content") or (inner or {}).get("preview")
    return str(content) if content else s3_object_content()


# ── AWS credential ─────────────────────────────────────────────────────────

def _gen_aws_credential(honeytoken: Dict[str, Any]) -> Dict[str, str]:
    honeytoken_id = honeytoken["honeytoken_id"]
    name = _name_for(honeytoken)
    cover = _cover(honeytoken)
    workspace_id = _workspace_literal(honeytoken)
    # Cloud-visible name mimics a real service account.
    user_name = cover

    hcl = f'''locals {{
  cover = "{cover}"
}}

# Deny-all service account: a stolen key can do nothing except reveal the thief.
resource "aws_iam_user" "user" {{
  name = "{user_name}"
  path = "/service/"

{_tags(honeytoken_id)}
}}

resource "aws_iam_user_policy" "deny_all" {{
  name = "DenyAll"
  user = aws_iam_user.user.name
  policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [{{
      Effect   = "Deny"
      Action   = "*"
      Resource = "*"
    }}]
  }})
}}

resource "aws_iam_access_key" "key" {{
  user = aws_iam_user.user.name
}}

output "access_key_id" {{
  value       = aws_iam_access_key.key.id
  description = "Access key id for {cover}."
}}

output "secret_access_key" {{
  value       = aws_iam_access_key.key.secret
  description = "Secret access key for {cover}."
  sensitive   = true
}}

# Read-only management events (e.g. sts:GetCallerIdentity) only reach a rule in
# this state.
resource "aws_cloudwatch_event_rule" "rule" {{
  name        = "{cover}-activity"
  description = "API activity for {cover}"
  state       = "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"

  event_pattern = jsonencode({{
    detail-type = ["AWS API Call via CloudTrail"]
    detail = {{
      userIdentity = {{
        type     = ["IAMUser"]
        userName = ["{user_name}"]
      }}
    }}
  }})
}}

{_event_target(honeytoken_id, workspace_id, "aws_cloudwatch_event_rule.rule", "iam")}'''
    return {f"{SERVICES_DIR}/{name}/main.tf": hcl}


# ── Fake API token (callback-based) ───────────────────────────────────────

def _gen_api_token(honeytoken: Dict[str, Any]) -> Dict[str, str]:
    honeytoken_id = honeytoken["honeytoken_id"]
    name = _name_for(honeytoken)
    cover = _cover(honeytoken)
    workspace_id = _workspace_literal(honeytoken)

    lambda_hcl = f'''locals {{
  cover = "{cover}"
}}

data "archive_file" "zip" {{
  type        = "zip"
  output_path = "${{path.module}}/{cover}.zip"

  source {{
    content  = <<PYTHON
import json, os, urllib.request

def handler(event, context):
    ctx = event.get("requestContext", {{}}).get("http", {{}})
    payload = {{
        "token_id": "{honeytoken_id}",
        "workspace_id": "{workspace_id}",
        "source_ip": ctx.get("sourceIp"),
        "user_agent": ctx.get("userAgent"),
        "service": "api_token_callback",
    }}
    target = os.environ.get("CALLBACK_URL", "")
    if target:
        req = urllib.request.Request(
            target,
            data=json.dumps(payload).encode(),
            headers={{"Content-Type": "application/json"}},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    return {{"statusCode": 401, "body": json.dumps({{"error": "unauthorized"}})}}
PYTHON
    filename = "handler.py"
  }}
}}

resource "aws_lambda_function" "receiver" {{
  function_name    = "{cover}-gateway"
  handler          = "handler.handler"
  runtime          = "python3.12"
  role             = aws_iam_role.lambda_role.arn
  filename         = data.archive_file.zip.output_path
  source_code_hash = data.archive_file.zip.output_base64sha256
  timeout          = 10

  environment {{
    variables = {{
      CALLBACK_URL = var.callback_url
    }}
  }}

{_tags(honeytoken_id)}
}}

resource "aws_lambda_function_url" "url" {{
  function_name      = aws_lambda_function.receiver.function_name
  authorization_type = "NONE"
}}

resource "aws_iam_role" "lambda_role" {{
  name = "{cover}-gateway-role"
  assume_role_policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [{{
      Effect    = "Allow"
      Principal = {{ Service = "lambda.amazonaws.com" }}
      Action    = "sts:AssumeRole"
    }}]
  }})
{_tags(honeytoken_id)}
}}

resource "aws_iam_role_policy_attachment" "basic" {{
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}}

output "callback_url" {{
  value       = aws_lambda_function_url.url.function_url
  description = "Function URL for {cover}."
}}
'''
    return {f"{SERVICES_DIR}/{name}/main.tf": lambda_hcl}


# ── DNS record ──────────────────────────────────────────────────────

def _gen_dns_record(honeytoken: Dict[str, Any]) -> Dict[str, str]:
    honeytoken_id = honeytoken["honeytoken_id"]
    name = _name_for(honeytoken)
    cover = _cover(honeytoken)
    resource_path = honeytoken.get("resource_path", "")
    zone_id = resource_path if resource_path.startswith("Z") else ""

    zone_block = (
        ""
        if zone_id
        else f'data "aws_route53_zone" "zone" {{\n  name = "{resource_path or "corp.local"}"\n}}\n'
    )
    zone_ref = f'"{zone_id}"' if zone_id else "data.aws_route53_zone.zone.zone_id"

    hcl = f'''locals {{
  cover = "{cover}"
}}

{zone_block}
resource "aws_route53_record" "this" {{
  zone_id = {zone_ref}
  name    = "{cover}-internal"
  type    = "A"
  ttl     = 300
  records = ["127.0.0.1"]
}}

# Resolver query logging so lookups are captured.
resource "aws_cloudwatch_log_group" "dns_logs" {{
  name              = "/dns/{cover}"
  retention_in_days = 30

{_tags(honeytoken_id)}
}}

resource "aws_route53_resolver_query_log_config" "this" {{
  name            = "{cover}-resolver"
  destination_arn = aws_cloudwatch_log_group.dns_logs.arn
}}

# CloudWatch alarms publish to SNS, which forwards to the queue.
resource "aws_sns_topic" "alerts" {{
  name = "{cover}-dns"

{_tags(honeytoken_id)}
}}

resource "aws_sns_topic_subscription" "to_queue" {{
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "sqs"
  endpoint  = var.alert_queue_arn
}}

resource "aws_cloudwatch_log_metric_filter" "query" {{
  name           = "{cover}-query"
  log_group_name = aws_cloudwatch_log_group.dns_logs.name
  pattern        = "{cover}-internal"

  metric_transformation {{
    name      = "{cover}-queries"
    namespace = "Custom/Services"
    value     = "1"
  }}
}}

resource "aws_cloudwatch_metric_alarm" "dns_query" {{
  alarm_name          = "{cover}-dns-query"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "{cover}-queries"
  namespace           = "Custom/Services"
  period              = 60
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "DNS queries for {cover}-internal"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}}
'''
    return {f"{SERVICES_DIR}/{name}/main.tf": hcl}


# ── Fake SSH key ───────────────────────────────────────────────────────────

def _gen_ssh_key(honeytoken: Dict[str, Any]) -> Dict[str, str]:
    honeytoken_id = honeytoken["honeytoken_id"]
    name = _name_for(honeytoken)
    cover = _cover(honeytoken)
    workspace_id = _workspace_literal(honeytoken)
    key_name = cover_label(name)

    hcl = f'''locals {{
  cover = "{cover}"
}}

resource "tls_private_key" "key" {{
  algorithm = "RSA"
  rsa_bits  = 4096
}}

resource "aws_key_pair" "this" {{
  key_name   = "{key_name}"
  public_key = tls_private_key.key.public_key_openssh

{_tags(honeytoken_id)}
}}

output "private_key" {{
  value       = tls_private_key.key.private_key_openssh
  description = "Private key for {cover}."
  sensitive   = true
}}

resource "aws_security_group" "access" {{
  name        = "{cover}-access"
  description = "Restricted access endpoint"

  ingress {{
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "SSH"
  }}

{_tags(honeytoken_id)}
}}

# Fires when this key pair is attached to an instance launch. (In-session SSH
# auth is not a CloudTrail event.)
resource "aws_cloudwatch_event_rule" "rule" {{
  name        = "{cover}-activity"
  description = "Key pair launch events"

  event_pattern = jsonencode({{
    source      = ["aws.ec2"]
    detail-type = ["AWS API Call via CloudTrail"]
    detail = {{
      eventSource = ["ec2.amazonaws.com"]
      requestParameters = {{
        keyName = ["{key_name}"]
      }}
    }}
  }})
}}

{_event_target(honeytoken_id, workspace_id, "aws_cloudwatch_event_rule.rule", "ssh")}'''
    return {f"{SERVICES_DIR}/{name}/main.tf": hcl}
