"""What a captain may read in the target account, declared in its manifest
(docs/specs/access-model.md §5, §8).

A manifest's ``permissions.aws.read`` is a tuple of ``Statement``s: the IAM
read actions the captain's code calls, with ``*`` wildcards per service so a
new describe call needs no IAM change. Three things are generated from it and
nothing is written by hand:

- the **read role's policy**: the union over the installed captains, plus the
  core block every install needs (``read_role_policy``);
- each captain's **session policy**: its own block, passed to AssumeRole so
  STS narrows the session to what the captain declared (``session_policy``);
- the **check** that the code calls only what the manifest declares
  (``aws_calls``, ``uncovered``): a static pass over the package for
  ``.client("<service>")`` and ``<verb>_<noun>(`` pairs, mapped to IAM action
  names through botocore's service model. ``compass validate`` and
  ``tests/captains/test_permissions.py`` run it.

Placeholders in resources: ``{account_id}``, ``{state_bucket}`` (one copy of
the statement per configured ``aws.state_buckets`` entry; none configured, no
statement), ``{read_role_arn}`` and ``{act_role_arn}`` (the two Compass roles).
"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

# ── the declaration ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Statement:
    actions: Tuple[str, ...]
    resources: Tuple[str, ...] = ("*",)
    conditions: Optional[Mapping[str, Any]] = None
    sid: Optional[str] = None


@dataclass(frozen=True)
class AwsPermissions:
    read: Tuple[Statement, ...] = ()        # statements of the read session policy
    actions: Tuple[str, ...] = ()           # action types the captain may propose (compass_core.actions)


@dataclass(frozen=True)
class Permissions:
    aws: Optional[AwsPermissions] = None
    github: Tuple[str, ...] = ()            # informative today; the GitHub client checks nothing
    egress: Tuple[str, ...] = ()            # informative today


READ_VERBS = (
    "Get", "List", "Describe", "Lookup", "Simulate", "Search", "Query", "Scan", "BatchGet", "Select",
    "Check", "Test", "View", "Report", "Discover", "Retrieve", "Export", "GenerateCredential",
)
_ACTION_RE = re.compile(r"^[a-z0-9-]+:[A-Za-z*]+$")
_VERB_RE = re.compile("^(" + "|".join(READ_VERBS) + ")")
# a read that is a download in disguise: never in a read block, the spec's decision D5
FORBIDDEN_READS = ("lambda:Get*", "lambda:GetFunction", "*:*", "*")
MANAGED_POLICY_LIMIT = 6144     # characters of a managed policy document
SESSION_POLICY_LIMIT = 2048     # plaintext characters of an inline session policy
CALLER_IDENTITY = "sts:GetCallerIdentity"   # every session may ask who it is; declared nowhere

_PLACEHOLDER_SAMPLE = {"account_id": "123456789012", "state_buckets": ["example-state"],
                       "read_role_arn": "arn:aws:iam::123456789012:role/service/svc-inventory-reader-sample",
                       "act_role_arn": "arn:aws:iam::123456789012:role/service/svc-ops-responder-sample"}


def permission_errors(permissions: Optional[Permissions]) -> List[str]:
    """What is wrong with a manifest's permissions block; empty when it is valid."""
    if permissions is None:
        return []
    problems: List[str] = []
    aws = permissions.aws
    if aws is not None:
        for index, statement in enumerate(aws.read):
            where = f"read[{index}]"
            if not statement.actions:
                problems.append(f"{where}: no actions")
            for action in statement.actions:
                if action in FORBIDDEN_READS:
                    problems.append(f"{where}: {action} is never a read (it downloads code or allows everything)")
                    continue
                if not _ACTION_RE.match(action):
                    problems.append(f"{where}: {action!r} is not <service>:<Action>")
                    continue
                verb = action.split(":", 1)[1]
                if verb == "*" or not _VERB_RE.match(verb):
                    problems.append(f"{where}: {action} is not a read-only verb ({', '.join(READ_VERBS)}); a write is an action")
                if action == "s3:GetObject" and any(r in ("*", "arn:aws:s3:::*") for r in statement.resources):
                    problems.append(f"{where}: s3:GetObject on * (state files hold secrets); name the bucket")
            if not statement.resources:
                problems.append(f"{where}: no resources")
        rendered = json.dumps(_document(render_statements(aws.read, **_PLACEHOLDER_SAMPLE)), separators=(",", ":"))
        if len(rendered) > MANAGED_POLICY_LIMIT:
            problems.append(f"the read block renders to {len(rendered)} characters; the limit is {MANAGED_POLICY_LIMIT}")
        for action_type in aws.actions:
            if not re.match(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$", action_type):
                problems.append(f"action type {action_type!r} is not lower_snake_case (or <captain>.<name>)")
    for scope in permissions.github:
        if not re.match(r"^[a-z_]+:(read|write)$", scope):
            problems.append(f"github scope {scope!r} is not <permission>:read|write")
    for host in permissions.egress:
        if not re.match(r"^[a-z0-9.*-]+$", host):
            problems.append(f"egress {host!r} is not a hostname")
    return problems


# ── rendering ─────────────────────────────────────────────────────────────────

def render_statements(
    statements: Iterable[Statement],
    *,
    account_id: Optional[str] = None,
    state_buckets: Sequence[str] = (),
    read_role_arn: Optional[str] = None,
    act_role_arn: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """IAM statements with the placeholders filled. A statement whose placeholder has no
    value is left out: no bucket configured, no bucket statement."""
    values = {"account_id": account_id, "read_role_arn": read_role_arn, "act_role_arn": act_role_arn}
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for statement in statements:
        resources: List[str] = []
        complete = True
        for resource in statement.resources:
            if "{state_bucket}" in resource:
                resources.extend(resource.replace("{state_bucket}", bucket) for bucket in state_buckets)
                continue
            for key, value in values.items():
                token = "{" + key + "}"
                if token in resource:
                    if not value:
                        complete = False
                    resource = resource.replace(token, str(value or ""))
            resources.append(resource)
        if not complete or not resources:
            continue
        rendered: Dict[str, Any] = {"Effect": "Allow", "Action": sorted(set(statement.actions)), "Resource": resources if len(resources) > 1 else resources[0]}
        if statement.sid:
            rendered = {"Sid": statement.sid, **rendered}
        if statement.conditions:
            rendered["Condition"] = dict(statement.conditions)
        key = json.dumps(rendered, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(rendered)
    return out


def _document(statements: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"Version": "2012-10-17", "Statement": statements}


def _render_context(config: Mapping[str, Any]) -> Dict[str, Any]:
    aws = config.get("aws") if isinstance(config.get("aws"), dict) else {}
    roles = aws.get("roles") if isinstance(aws.get("roles"), dict) else {}
    buckets = aws.get("state_buckets") if isinstance(aws.get("state_buckets"), list) else []
    return {
        "account_id": aws.get("account_id") or None,
        "state_buckets": [str(b) for b in buckets if b],
        "read_role_arn": roles.get("read") or None,
        "act_role_arn": roles.get("act") or None,
    }


def captain_statements(permissions: Optional[Permissions], config: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """One captain's declared reads, rendered for this workspace."""
    if permissions is None or permissions.aws is None:
        return []
    return render_statements(permissions.aws.read, **_render_context(config))


def session_policy(permissions: Optional[Permissions], config: Mapping[str, Any]) -> Optional[str]:
    """The inline policy an AssumeRole narrows a captain's read session to: its declared
    reads plus GetCallerIdentity. None when it is too large to pass inline (then the
    captain's managed policy, ``aws.managed_policies[<captain>]``, is the session's
    PolicyArns instead)."""
    statements = captain_statements(permissions, config)
    statements.insert(0, {"Effect": "Allow", "Action": CALLER_IDENTITY, "Resource": "*"})
    text = json.dumps(_document(statements), separators=(",", ":"))
    return text if len(text) <= SESSION_POLICY_LIMIT else None


def read_role_policy(manifests: Iterable[Any], config: Mapping[str, Any]) -> Dict[str, Any]:
    """The read role's policy: the union of every installed captain's declared reads (the
    core's block, which carries the self-audit's IAM reads, included)."""
    statements: List[Dict[str, Any]] = [{"Sid": "CallerIdentity", "Effect": "Allow", "Action": CALLER_IDENTITY, "Resource": "*"}]
    seen: Set[str] = set()
    for manifest in manifests:
        for statement in captain_statements(getattr(manifest, "permissions", None), config):
            key = json.dumps(statement, sort_keys=True)
            if key not in seen:
                seen.add(key)
                statements.append(statement)
    return _document(statements)


def declared_actions(permissions: Optional[Permissions]) -> List[str]:
    if permissions is None or permissions.aws is None:
        return []
    return sorted({a for s in permissions.aws.read for a in s.actions})


def covers(declared: Iterable[str], action: str) -> bool:
    """Whether a declared read (``ecs:Describe*``) covers an IAM action (``ecs:DescribeServices``)."""
    if action == CALLER_IDENTITY:
        return True
    return any(fnmatch.fnmatchcase(action, pattern) for pattern in declared)


# ── the code against the manifest ─────────────────────────────────────────────

@dataclass(frozen=True)
class AwsCall:
    file: str
    line: int
    service: str
    operation: str          # botocore operation name, e.g. DescribeServices
    action: str             # the IAM action, e.g. ecs:DescribeServices
    control: bool           # made on sessions.control(): the control account's own resources, not the target

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.action}"


# botocore service name → IAM action prefix, where the signing name is not it
_PREFIXES = {"resourcegroupstaggingapi": "tag", "cloudwatch": "cloudwatch", "stepfunctions": "states",
             "elbv2": "elasticloadbalancing", "elb": "elasticloadbalancing", "logs": "logs", "events": "events"}
# operations whose IAM action has another name
_RENAMED = {("s3", "ListBuckets"): "ListAllMyBuckets", ("s3", "ListObjects"): "ListBucket", ("s3", "ListObjectsV2"): "ListBucket",
            ("s3", "HeadBucket"): "ListBucket", ("s3", "HeadObject"): "GetObject", ("s3", "GetBucketLocation"): "GetBucketLocation"}
_NOT_OPERATIONS = {"get_paginator", "can_paginate", "get_waiter", "generate_presigned_url", "generate_presigned_post", "close", "meta", "exceptions"}

_models: Dict[str, Optional[Dict[str, str]]] = {}


def _operations(service: str) -> Optional[Dict[str, str]]:
    """method name → operation name for a botocore service, None for a name botocore does not know."""
    if service not in _models:
        try:
            from botocore import xform_name
            from botocore.loaders import create_loader
            from botocore.model import ServiceModel

            model = ServiceModel(create_loader().load_service_model(service, "service-2"), service_name=service)
            table = {xform_name(op): op for op in model.operation_names}
            table["__prefix__"] = _PREFIXES.get(service) or model.signing_name or service
            _models[service] = table
        except Exception:  # noqa: BLE001 - an unknown service is reported as such, not as a crash
            _models[service] = None
    return _models[service]


def iam_action(service: str, operation: str) -> Optional[str]:
    table = _operations(service)
    if table is None:
        return None
    name = _RENAMED.get((service, operation), operation)
    return f"{table['__prefix__']}:{name}"


def _client_call(node: ast.AST) -> Optional[Tuple[str, bool]]:
    """(service, on the control session) for a ``<x>.client("svc", ...)`` call anywhere in ``node``."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "client" and sub.args:
            first = sub.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value, _is_control(sub.func.value)
    return None


def _is_control(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "control":
            return True
        if isinstance(sub, ast.Name) and sub.id == "control":
            return True
    return False


def _bind_name(target: ast.AST) -> Optional[str]:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
        return f"self.{target.attr}"
    return None


def _bindings(node: ast.AST, into: Dict[str, Tuple[str, bool]]) -> None:
    for sub in ast.walk(node):
        if isinstance(sub, (ast.Assign, ast.AnnAssign)) and sub.value is not None:
            client = _client_call(sub.value)
            if client is None:
                continue
            for target in (sub.targets if isinstance(sub, ast.Assign) else [sub.target]):
                name = _bind_name(target)
                if name:
                    into[name] = client


def _calls(scope: ast.AST, bindings: Dict[str, Tuple[str, bool]], rel: str) -> List[AwsCall]:
    found: List[AwsCall] = []
    for node in ast.walk(scope):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        receiver, method = node.func.value, node.func.attr
        client: Optional[Tuple[str, bool]] = None
        name = _bind_name(receiver)
        if name and name in bindings:
            client = bindings[name]
        elif isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Attribute) and receiver.func.attr == "client":
            client = _client_call(receiver)
        elif isinstance(receiver, ast.Name):
            # a client passed in as a parameter: its name says the service (s3_client, iam, ecs)
            guess = receiver.id.removesuffix("_client")
            if _operations(guess) is not None:
                client = (guess, False)
        if client is None:
            continue
        service, control = client
        if method == "get_paginator":
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            method = str(node.args[0].value)
        elif method in _NOT_OPERATIONS:
            continue
        operation = (_operations(service) or {}).get(method)
        if operation is None:
            continue
        action = iam_action(service, operation) or f"{service}:{operation}"
        found.append(AwsCall(file=rel, line=node.lineno, service=service, operation=operation, action=action, control=control))
    return found


def _calls_in_module(path: Path, rel: str) -> List[AwsCall]:
    """Bindings are scoped per function (``client = session.client("lambda")`` in one
    collector, ``client = session.client("dynamodb")`` in the next); ``self.<attr>``
    bindings and module-level ones are seen everywhere."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    shared: Dict[str, Tuple[str, bool]] = {}
    _bindings(tree, shared)
    shared = {k: v for k, v in shared.items() if k.startswith("self.")}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            _bindings(node, shared)
    found: List[AwsCall] = []
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for function in functions:
        local = dict(shared)
        _bindings(function, local)
        found.extend(_calls(function, local, rel))
    seen: Set[Tuple[str, int, str]] = set()
    unique: List[AwsCall] = []
    for call in found:  # a nested function is walked by itself and by its parent
        key = (call.file, call.line, call.action)
        if key not in seen:
            seen.add(key)
            unique.append(call)
    return unique


def aws_calls(package_dir: Path, *, skip: Sequence[str] = ("tests", "actions", "listener")) -> List[AwsCall]:
    """Every AWS API call a package makes, by file and line. ``skip`` names directories
    left out: the package's tests, its write actions (checked against the action's own
    statements) and a listener that runs in the target account with its own role."""
    root = Path(package_dir)
    found: List[AwsCall] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root.parent).as_posix()
        if any(part in skip for part in path.relative_to(root).parts[:-1]):
            continue
        found.extend(_calls_in_module(path, rel))
    return found


def uncovered(permissions: Optional[Permissions], calls: Iterable[AwsCall]) -> List[AwsCall]:
    """The target-account calls the manifest's read block does not cover."""
    declared = declared_actions(permissions)
    return [c for c in calls if not c.control and not covers(declared, c.action)]


# ── what a captain shows before install ───────────────────────────────────────

def describe(permissions: Optional[Permissions]) -> Dict[str, Any]:
    """The permissions block as the Captains page and the catalog show it: reads grouped by
    service, the action types, GitHub scopes and egress."""
    if permissions is None:
        return {"declared": False, "reads": {}, "actions": [], "github": [], "egress": []}
    reads: Dict[str, List[str]] = {}
    scoped: List[Dict[str, Any]] = []
    for statement in (permissions.aws.read if permissions.aws else ()):
        for action in statement.actions:
            service, verb = action.split(":", 1)
            reads.setdefault(service, [])
            if verb not in reads[service]:
                reads[service].append(verb)
        if tuple(statement.resources) != ("*",):
            scoped.append({"actions": list(statement.actions), "resources": list(statement.resources)})
    from compass_core import actions as _actions

    return {
        "declared": True,
        "reads": {service: sorted(verbs) for service, verbs in sorted(reads.items())},
        "scoped": scoped,
        "actions": [_actions.describe_action(a) for a in (permissions.aws.actions if permissions.aws else ())],
        "github": list(permissions.github),
        "egress": list(permissions.egress),
    }

