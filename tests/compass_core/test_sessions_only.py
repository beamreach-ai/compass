"""The rule of docs/specs/access-model.md §7: nothing outside the broker constructs a
boto3 session. An AST walk over the open packages; ruff cannot express this."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("compass_core", "compass_api", "captains", "infra_map")
ALLOWED = {
    "compass_core/sessions.py",            # the broker
    "compass_core/providers/aws.py",       # where AssumeRole moves when a second provider arrives
}
ALLOWED_PREFIXES = (
    "captains/honeypot/listener/",         # runs as an ECS task in the target account, with its own task role and no Compass config
)
CONSTRUCTORS = {"Session", "client", "resource"}


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def offenders(source: str) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if _root_name(node.func) == "boto3" and node.func.attr in CONSTRUCTORS:
                found.append((node.lineno, f"boto3.{node.func.attr}("))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in ("boto3", "boto3.session") and any(a.name in CONSTRUCTORS for a in node.names):
                found.append((node.lineno, f"from {module} import ..."))
            if module == "botocore.session" or (module == "botocore" and any(a.name == "session" for a in node.names)):
                found.append((node.lineno, "botocore.session"))
        elif isinstance(node, ast.Import):
            if any(a.name == "botocore.session" for a in node.names):
                found.append((node.lineno, "import botocore.session"))
    return found


def test_only_the_broker_builds_sessions():
    violations = []
    for package in PACKAGES:
        for path in sorted((ROOT / package).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in ALLOWED or rel.startswith(ALLOWED_PREFIXES) or "/tests/" in f"/{rel}":
                continue
            for lineno, what in offenders(path.read_text()):
                violations.append(f"{rel}:{lineno}: {what}")
    assert not violations, "build sessions through compass_core.sessions (control() / target()):\n" + "\n".join(violations)


def test_the_walk_sees_the_shapes_it_must_refuse():
    source = (
        "import boto3\n"
        "import botocore.session\n"
        "from boto3 import Session\n"
        "a = boto3.Session()\n"
        "b = boto3.client('sts')\n"
        "c = boto3.session.Session()\n"
        "d = boto3.resource('dynamodb').Table('t')\n"
        "from boto3.dynamodb.conditions import Key  # fine: not a session\n"
    )
    assert [line for line, _ in offenders(source)] == [2, 3, 4, 5, 6, 7]
