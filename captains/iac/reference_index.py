"""Builds the value -> reference index used to rewire generated HCL.

`terraform plan -generate-config-out` emits flat literals:

    vpc_id = "vpc-0e040db1f49390291"

To turn that into `module.beamreach-demo-vpc.vpc_id` we need to know which
expressions, evaluated in the target directory, currently produce that value.
This module answers that question.

Two properties shape the design.

**Scope is one root module, not the repository.** Terraform's reference
namespace is a single directory with its own state; a literal in
`environments/demo` can only be rewired to something addressable from
`environments/demo`. A sibling environment is unreachable without
`terraform_remote_state`, which is out of scope.

**Direction is inverted.** Terraform can evaluate an expression into a value;
we need the value to find the expression. So candidates are enumerated,
evaluated, and the mapping is inverted.

Nothing here is persisted. A stale index does not fail loudly — it rewires a
literal to a reference that no longer evaluates to that value, producing config
that is quietly wrong, which is worse than leaving the literal alone.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from captains.iac.tf_runner import TerraformRunner

logger = logging.getLogger("iac-reference-index")

# Values shorter than this match by coincidence far too often — ports, "main",
# "demo", "true". Long enough to be specific, short enough to keep "us-east-1".
MIN_VALUE_LENGTH = 6

# Attributes of root-level resources worth indexing. Indexing every attribute
# invites false positives from free-text fields like `description`; identifiers
# are what generated config actually needs to point at.
_IDENTIFYING_ATTRS = {"id", "arn", "name", "bucket", "url", "endpoint"}
_IDENTIFYING_SUFFIXES = ("_id", "_arn", "_name", "_url", "_endpoint")

# Lower sorts first. Locals and variables express the repo's own intent, so they
# read best; a data source implies an extra lookup, so it loses to a direct
# reference that is already in the configuration.
_KIND_RANK = {"local": 0, "var": 1, "module_output": 2, "resource": 3, "data": 4}


@dataclass(frozen=True)
class Reference:
    expression: str
    kind: str

    @property
    def rank(self) -> int:
        return _KIND_RANK.get(self.kind, 99)


class ReferenceIndex:
    """Maps a literal value to the best expression producing it."""

    def __init__(self) -> None:
        self._by_value: Dict[str, Reference] = {}
        self.ambiguous: Dict[str, List[str]] = {}

    def add(self, value: Any, reference: Reference) -> None:
        key = _index_key(value)
        if key is None:
            return
        current = self._by_value.get(key)
        if current is None:
            self._by_value[key] = reference
            return
        if current.expression == reference.expression:
            return
        # Several references legitimately produce the same value —
        # local.aws_region and data.aws_region.current.name both give
        # "us-east-1". Keep the preferred one, but remember the alternatives so
        # the choice can be surfaced to a reviewer rather than made silently.
        self.ambiguous.setdefault(key, [current.expression]).append(reference.expression)
        if _better(reference, current):
            self._by_value[key] = reference

    def lookup(self, value: Any) -> Optional[Reference]:
        key = _index_key(value)
        return self._by_value.get(key) if key else None

    def alternatives(self, value: Any) -> List[str]:
        key = _index_key(value)
        return list(self.ambiguous.get(key, [])) if key else []

    def __len__(self) -> int:
        return len(self._by_value)

    def __contains__(self, value: Any) -> bool:
        return self.lookup(value) is not None


def _better(candidate: Reference, incumbent: Reference) -> bool:
    if candidate.rank != incumbent.rank:
        return candidate.rank < incumbent.rank
    return len(candidate.expression) < len(incumbent.expression)


def _index_key(value: Any) -> Optional[str]:
    """Normalise a value into an index key, or None if it should not be indexed.

    Only scalars are admitted, and only ones specific enough that an exact match
    is meaningful. Booleans are excluded outright: `true` matching `var.enabled`
    would rewrite unrelated attributes across the file.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) < MIN_VALUE_LENGTH:
        return None
    if value.lower() in {"true", "false", "null", "managed by terraform"}:
        return None
    return value


# --------------------------------------------------------------------- locals

_LOCALS_BLOCK = re.compile(r"^\s*locals\s*\{", re.MULTILINE)
_VARIABLE_BLOCK = re.compile(r'^\s*variable\s+"([^"]+)"\s*\{', re.MULTILINE)
_ASSIGNMENT = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=')


def _top_level_local_names(text: str) -> List[str]:
    """Names assigned directly inside `locals { }` blocks.

    Deliberately not a general HCL parser — it only needs the keys at brace
    depth one, which avoids taking a dependency the worker image does not carry.
    Nested maps are skipped, since only scalars are indexable anyway.
    """
    names: List[str] = []
    for match in _LOCALS_BLOCK.finditer(text):
        depth = 0
        i = match.end() - 1
        line_start = match.end()
        while i < len(text):
            char = text[i]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            elif char == "\n":
                line_end = text.find("\n", i + 1)
                line = text[i + 1 : line_end if line_end != -1 else len(text)]
                if depth == 1:
                    found = _ASSIGNMENT.match(line)
                    if found:
                        names.append(found.group(1))
            i += 1
        del line_start
    return names


_OUTPUT_BLOCK = re.compile(r'^\s*output\s+"([^"]+)"\s*\{', re.MULTILINE)


def module_output_names(cwd: str) -> Dict[str, List[str]]:
    """Output names declared by each installed module, keyed by call name.

    Individual outputs have to be probed one at a time. Evaluating a module as a
    whole object looked attractive — one expression yielding every output — but
    a module's outputs are not stored in state, and a single member that is
    unknown renders the entire object `(known after apply)`, losing the ones
    that did resolve. `module.beamreach-demo-vpc.vpc_id` resolves cleanly;
    `module.beamreach-demo-vpc` does not.

    `.terraform/modules/modules.json` is written by `init` and maps every
    installed module — registry, git or local — to the directory it was
    installed into, so this needs no HCL parser and no knowledge of the source.
    """
    manifest = Path(cwd) / ".terraform" / "modules" / "modules.json"
    if not manifest.exists():
        logger.info("no module manifest at %s; module outputs will not be indexed", manifest)
        return {}

    try:
        entries = json.loads(manifest.read_text(encoding="utf-8")).get("Modules") or []
    except (OSError, ValueError) as exc:
        logger.warning("could not read module manifest: %s", exc)
        return {}

    found: Dict[str, List[str]] = {}
    for entry in entries:
        key = entry.get("Key") or ""
        # Only direct children of the root are referenceable from the root;
        # nested modules appear as "parent.child" and are skipped. The root
        # itself has an empty key.
        if not key or "." in key:
            continue
        module_dir = Path(cwd) / (entry.get("Dir") or "")
        if not module_dir.is_dir():
            continue
        names: List[str] = []
        for tf_file in sorted(module_dir.glob("*.tf")):
            try:
                names.extend(_OUTPUT_BLOCK.findall(tf_file.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
        if names:
            found[key] = sorted(set(names))
    return found


def _read_root_tf(cwd: str) -> str:
    parts: List[str] = []
    for path in sorted(Path(cwd).glob("*.tf")):
        try:
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            logger.debug("could not read %s", path)
    return "\n".join(parts)


# ---------------------------------------------------------------------- state




def _index_root_resources(index: ReferenceIndex, state: Dict[str, Any], skip: Set[str]) -> None:
    root = ((state.get("values") or {}).get("root_module")) or {}
    for resource in root.get("resources") or []:
        address = resource.get("address") or ""
        if not address or address in skip:
            continue
        kind = "data" if resource.get("mode") == "data" else "resource"
        for attr, value in (resource.get("values") or {}).items():
            if attr in _IDENTIFYING_ATTRS or attr.endswith(_IDENTIFYING_SUFFIXES):
                index.add(value, Reference(f"{address}.{attr}", kind))


# ---------------------------------------------------------------------- build


def _add_scalar_or_nested(index: "ReferenceIndex", value: Any, expression: str, kind: str) -> None:
    """Index a value, descending one level into maps.

    A local is often a map (`local.docker_images["multistage"].repo_name`), and
    those leaves are exactly the kind of thing generated config repeats as a
    literal. One level is enough to catch them without generating expressions
    nobody would ever have written by hand.
    """
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(nested, dict):
                for leaf_key, leaf in nested.items():
                    index.add(leaf, Reference(f'{expression}["{key}"].{leaf_key}', kind))
            else:
                index.add(nested, Reference(f'{expression}["{key}"]', kind))
        return
    index.add(value, Reference(expression, kind))


def build_reference_index(
    tf: TerraformRunner,
    cwd: str,
    *,
    exclude_addresses: Sequence[str] = (),
) -> ReferenceIndex:
    """Build the index for one root module.

    `exclude_addresses` are the resources being imported. They must never become
    rewiring targets: a resource cannot reference itself, and pointing one member
    of an import batch at another by literal creates ordering hazards.
    """
    index = ReferenceIndex()
    skip = set(exclude_addresses)

    state = tf.show_state_json(cwd)
    _index_root_resources(index, state, skip)

    # Everything the state cannot answer resolves in one probe plan. Module
    # outputs are enumerated per name rather than per module, because a whole
    # module object collapses to unknown if any single output is unknown.
    outputs_by_module = module_output_names(cwd)
    text = _read_root_tf(cwd)
    local_names = _top_level_local_names(text)
    var_names = [m.group(1) for m in _VARIABLE_BLOCK.finditer(text)]

    expressions: List[str] = []
    kinds: Dict[str, str] = {}
    for call, output_names in outputs_by_module.items():
        for output_name in output_names:
            expr = f"module.{call}.{output_name}"
            expressions.append(expr)
            kinds[expr] = "module_output"
    for name in local_names:
        expressions.append(f"local.{name}")
        kinds[f"local.{name}"] = "local"
    for name in var_names:
        expressions.append(f"var.{name}")
        kinds[f"var.{name}"] = "var"

    resolved = tf.probe_expressions(cwd, expressions) if expressions else {}
    for expr, value in resolved.items():
        _add_scalar_or_nested(index, value, expr, kinds.get(expr, "local"))

    logger.info(
        "reference index: %d values, %d ambiguous, from %d module(s) and %d probed expression(s)",
        len(index),
        len(index.ambiguous),
        len(outputs_by_module),
        len(expressions),
    )
    return index
