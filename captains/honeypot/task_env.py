"""HP-05: put a honeytoken into an existing ECS task definition's environment.

The plant PR edits the customer's own `aws_ecs_task_definition` (placement
type `ecs_task_env`): the named container gets one `environment` entry per
variable, each wired to the token module's output, so nothing secret is
written into the repository. Decommission removes the same entries.

hcl2 parses but cannot serialise, so the edit is textual and minimal: hcl2
validates the resource first, a small scanner finds the container's
`environment = [...]` list inside `jsonencode([...])`, and lines are inserted
or removed there. Anything that is not a literal `jsonencode([...])`
(a `file()`, a variable, a `templatefile()`) is refused with instructions,
because Compass cannot edit what it cannot see.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

import hcl2

from captains.honeypot.classes import normalize_class

TASK_DEF_TYPE = "aws_ecs_task_definition"


class TaskEnvError(ValueError):
    """The task definition cannot be edited automatically; the message says what to do."""


# Variable names the workload sees, and the module output each one reads.
# Names look like a backup job's configuration, which is what makes them worth stealing.
_CLASS_ENV: Dict[str, List[Tuple[str, str]]] = {
    "aws_credential": [
        ("BACKUP_AWS_ACCESS_KEY_ID", "access_key_id"),
        ("BACKUP_AWS_SECRET_ACCESS_KEY", "secret_access_key"),
    ],
    "db_credential": [
        ("DB_HOST", "host"),
        ("DB_PORT", ""),  # literal below
        ("DB_USER", "user"),
        ("DB_PASSWORD", "password"),
    ],
}
_LITERALS = {"DB_PORT": '"5432"'}


def env_entries(honeytoken_class: str, module_name: str) -> List[Tuple[str, str]]:
    """[(variable name, HCL expression)] for a class; empty when the class has no env form."""
    out = []
    for var, output in _CLASS_ENV.get(normalize_class(honeytoken_class or ""), []):
        out.append((var, _LITERALS.get(var) or f"module.{module_name}.{output}"))
    return out


# ── locating the resource ──────────────────────────────────────────────────

def _unquote(key: str) -> str:
    return key[1:-1] if len(key) >= 2 and key[0] == key[-1] == '"' else key


def task_definitions(text: str) -> Dict[str, str]:
    """{resource name: raw container_definitions expression} for every task definition in a file."""
    try:
        parsed = hcl2.loads(text or "")
    except Exception:  # noqa: BLE001 - an unparsable file simply has no candidates
        return {}
    out: Dict[str, str] = {}
    for block in parsed.get("resource", []):
        for rtype, named in block.items():
            if _unquote(rtype) != TASK_DEF_TYPE:
                continue
            for rname, body in named.items():
                out[_unquote(rname)] = str(body.get("container_definitions", ""))
    return out


def find_task_definition(files: Dict[str, str], *, container: str, tf_address: Optional[str] = None) -> Tuple[str, str]:
    """(file path, resource name) of the task definition to edit.

    With `tf_address` (`aws_ecs_task_definition.web`) that resource; otherwise the
    one task definition whose container_definitions names `container`."""
    wanted = None
    if tf_address:
        parts = tf_address.split(".")
        if len(parts) != 2 or parts[0] != TASK_DEF_TYPE:
            raise TaskEnvError(f"placement.tf_address must look like {TASK_DEF_TYPE}.<name> (got {tf_address!r})")
        wanted = parts[1]
    hits = []
    for path, text in sorted(files.items()):
        for name, _defs in task_definitions(text).items():
            if wanted is not None:
                if name == wanted:
                    hits.append((path, name))
            elif re.search(r'\bname\s*=\s*"' + re.escape(container) + '"', _jsonencode_body(text, name) or ""):
                hits.append((path, name))
    if not hits:
        target = tf_address or f'a task definition with a container named "{container}"'
        raise TaskEnvError(f"Could not find {target} in the Terraform root. Set placement.tf_address.")
    if len(hits) > 1:
        names = ", ".join(f"{TASK_DEF_TYPE}.{n} ({p})" for p, n in hits)
        raise TaskEnvError(f"More than one task definition matches: {names}. Set placement.tf_address.")
    return hits[0]


# ── the scanner ────────────────────────────────────────────────────────────

def _skip_string(text: str, i: int) -> int:
    """`text[i]` is an opening quote; return the index after the closing one (handles ${…} and escapes)."""
    i += 1
    depth = 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if text.startswith("${", i):
            depth += 1
            i += 2
            continue
        if c == "}" and depth:
            depth -= 1
        elif c == '"' and not depth:
            return i + 1
        i += 1
    raise TaskEnvError("Unterminated string in the task definition")


def _skip_comment(text: str, i: int) -> int:
    if text.startswith("#", i) or text.startswith("//", i):
        end = text.find("\n", i)
        return len(text) if end < 0 else end
    if text.startswith("/*", i):
        end = text.find("*/", i + 2)
        return len(text) if end < 0 else end + 2
    return i


def _match(text: str, open_idx: int) -> int:
    """Index of the bracket closing the one at `open_idx` ({, [ or ()."""
    pairs = {"{": "}", "[": "]", "(": ")"}
    stack = [pairs[text[open_idx]]]
    i = open_idx + 1
    while i < len(text):
        j = _skip_comment(text, i)
        if j != i:
            i = j
            continue
        c = text[i]
        if c == '"':
            i = _skip_string(text, i)
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return i
        i += 1
    raise TaskEnvError("Unbalanced brackets in the task definition")


def _resource_span(text: str, name: str) -> Tuple[int, int]:
    m = re.search(r'resource\s+"' + TASK_DEF_TYPE + r'"\s+"' + re.escape(name) + r'"\s*\{', text)
    if not m:
        raise TaskEnvError(f"{TASK_DEF_TYPE}.{name} not found")
    start = m.end() - 1
    return start, _match(text, start)


def _jsonencode_span(text: str, name: str) -> Tuple[int, int]:
    """(index of the `[`, index of its `]`) of `container_definitions = jsonencode([ … ])`."""
    rs, re_ = _resource_span(text, name)
    body = text[rs:re_]
    m = re.search(r"\bcontainer_definitions\s*=\s*", body)
    if not m:
        raise TaskEnvError(f"{TASK_DEF_TYPE}.{name} has no container_definitions")
    after = rs + m.end()
    lit = re.match(r"jsonencode\s*\(\s*\[", text[after:])
    if not lit:
        found = text[after:after + 40].split("\n")[0]
        raise TaskEnvError(
            f"{TASK_DEF_TYPE}.{name}.container_definitions is `{found}…`, not a literal jsonencode([...]); "
            "Compass cannot edit it. Add the variables listed in the PR description to the container by hand."
        )
    open_idx = after + lit.end() - 1
    return open_idx, _match(text, open_idx)


def _jsonencode_body(text: str, name: str) -> Optional[str]:
    try:
        a, b = _jsonencode_span(text, name)
    except TaskEnvError:
        return None
    return text[a:b + 1]


def _objects(text: str, start: int, end: int) -> List[Tuple[int, int]]:
    """Top-level `{…}` spans between start and end."""
    out, i = [], start
    while i < end:
        j = _skip_comment(text, i)
        if j != i:
            i = j
            continue
        c = text[i]
        if c == '"':
            i = _skip_string(text, i)
            continue
        if c in "[(":
            i = _match(text, i) + 1
            continue
        if c == "{":
            close = _match(text, i)
            out.append((i, close))
            i = close + 1
            continue
        i += 1
    return out


def _top_level_key(text: str, start: int, end: int, key: str) -> Optional[re.Match]:
    """`key =` at depth 1 of the object spanning start..end (not inside a nested value)."""
    i = start + 1
    while i < end:
        j = _skip_comment(text, i)
        if j != i:
            i = j
            continue
        c = text[i]
        if c == '"':
            i = _skip_string(text, i)
            continue
        if c in "[{(":
            i = _match(text, i) + 1
            continue
        m = re.compile(r"\b" + re.escape(key) + r"\s*=\s*").match(text, i)
        if m and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            return m
        i += 1
    return None


def _container_span(text: str, name: str, container: str) -> Tuple[int, int]:
    a, b = _jsonencode_span(text, name)
    for os_, oe in _objects(text, a + 1, b):
        m = _top_level_key(text, os_, oe, "name")
        if m and re.match(r'"' + re.escape(container) + r'"', text[m.end():]):
            return os_, oe
    raise TaskEnvError(f'{TASK_DEF_TYPE}.{name} has no container named "{container}"')


def _line_indent(text: str, idx: int) -> str:
    line_start = text.rfind("\n", 0, idx) + 1
    return re.match(r"[ \t]*", text[line_start:]).group(0)


def _entry_spans(text: str, a: int, b: int) -> List[Tuple[str, int, int]]:
    """[(variable name, start, end)] of `{ name = "X", value = … }` entries inside a list a..b."""
    out = []
    for os_, oe in _objects(text, a + 1, b):
        m = _top_level_key(text, os_, oe, "name")
        n = re.match(r'"([^"]*)"', text[m.end():]) if m else None
        if n:
            out.append((n.group(1), os_, oe))
    return out


def _render(var: str, expr: str) -> str:
    return f'{{ name = "{var}", value = {expr} }}'


# ── the edits ──────────────────────────────────────────────────────────────

def inject_env(text: str, *, resource: str, container: str, entries: Sequence[Tuple[str, str]]) -> str:
    """Add (or update) `entries` in the container's environment. Idempotent."""
    if not entries:
        return text
    text = remove_env(text, resource=resource, container=container, names=[v for v, _ in entries])
    cs, ce = _container_span(text, resource, container)
    m = _top_level_key(text, cs, ce, "environment")
    if m is None:
        # No environment yet: add one right after the container's `name = "…"` line.
        nm = _top_level_key(text, cs, ce, "name")
        line_end = text.find("\n", nm.end())
        indent = _line_indent(text, nm.start())
        inner = indent + "  "
        block = (
            f"\n{indent}environment = [\n"
            + ",\n".join(f"{inner}{_render(v, e)}" for v, e in entries)
            + f"\n{indent}]"
        )
        return text[:line_end] + block + text[line_end:]
    la = m.end()
    if text[la] != "[":
        raise TaskEnvError(f"{TASK_DEF_TYPE}.{resource}: container {container}'s environment is not a literal list")
    lb = _match(text, la)
    existing = _entry_spans(text, la, lb)
    key_indent = _line_indent(text, m.start())
    inner = _line_indent(text, existing[0][1]) if existing else key_indent + "  "
    new_lines = ",\n".join(f"{inner}{_render(v, e)}" for v, e in entries)
    if existing:
        last_end = existing[-1][2] + 1
        return text[:last_end] + ",\n" + new_lines + text[last_end:]
    return text[:la + 1] + "\n" + new_lines + "\n" + key_indent + text[lb:]


def remove_env(text: str, *, resource: str, container: str, names: Sequence[str]) -> str:
    """Drop entries with these names from the container's environment (and their commas/lines)."""
    wanted = set(names)
    while True:
        cs, ce = _container_span(text, resource, container)
        m = _top_level_key(text, cs, ce, "environment")
        if m is None or text[m.end()] != "[":
            return text
        la = m.end()
        lb = _match(text, la)
        spans = _entry_spans(text, la, lb)
        hit = next((s for s in spans if s[0] in wanted), None)
        if hit is None:
            return text
        _, os_, oe = hit
        idx = spans.index(hit)
        # The entry, its leading indentation and newline, and one separating comma:
        # its own trailing comma, or (for the last entry) the previous entry's.
        start = os_
        while text[start - 1] in " \t":
            start -= 1
        if text[start - 1] == "\n":
            start -= 1
        end = oe + 1
        after = re.match(r"[ \t]*,", text[end:])
        if after:
            end += after.end()
        elif idx > 0:
            start = spans[idx - 1][2] + 1
        text = text[:start] + text[end:]


def env_names(text: str, *, resource: str, container: str) -> List[str]:
    cs, ce = _container_span(text, resource, container)
    m = _top_level_key(text, cs, ce, "environment")
    if m is None or text[m.end()] != "[":
        return []
    return [n for n, _, _ in _entry_spans(text, m.end(), _match(text, m.end()))]
