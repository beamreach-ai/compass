"""Cleans up configuration produced by `terraform plan -generate-config-out`.

Terraform's generator is faithful to what AWS returned, not to what its own
provider will accept. Three things come out of it that must not reach a pull
request:

* **Read-only attributes.** `tags_all` is computed — the provider derives it
  and rejects it as an input. It appears anyway.
* **Values the schema refuses.** `recovery_period_in_days = 0` is emitted for a
  DynamoDB table whose point-in-time recovery is disabled, and the provider
  requires 1-35. The plan fails on it.
* **Null filler.** `name_prefix = null`, `revoke_rules_on_delete = null` and
  friends are harmless but make a reviewer wade through noise to find the
  substance.

The first and third are fixed from the provider schema, which is already
fetched for sensitive-value scrubbing. The second cannot be — it is a value
constraint, not a shape — so it is fixed by asking terraform what it objects to
and removing exactly those lines, which is both more general and more honest
than encoding a list of known-bad attributes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Sequence, Set

logger = logging.getLogger("iac-normalize")

_ASSIGNMENT = re.compile(r'^(?P<indent>\s*)(?P<attr>[A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(?P<value>.*)$')
_RESOURCE_HEADER = re.compile(r'^\s*resource\s+"(?P<type>[^"]+)"\s+"[^"]+"\s*\{')


@dataclass
class NormalizeResult:
    text: str
    dropped: List[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.dropped)


def read_only_attributes(schema: Dict[str, Any]) -> Dict[str, Set[str]]:
    """Attributes the provider computes and will not accept as input.

    `computed` alone is not enough to disqualify an attribute: most optional
    arguments are also computed, meaning "you may set this, and if you don't I
    will derive it". Only attributes that are computed and *not* optional are
    genuinely read-only.
    """
    out: Dict[str, Set[str]] = {}
    for provider in (schema.get("provider_schemas") or {}).values():
        for res_type, res_schema in (provider.get("resource_schemas") or {}).items():
            attrs = ((res_schema.get("block") or {}).get("attributes")) or {}
            flagged = {
                name
                for name, spec in attrs.items()
                if spec.get("computed") and not spec.get("optional")
            }
            if flagged:
                out.setdefault(res_type, set()).update(flagged)
    return out


def _filter_lines(hcl_text: str, should_drop: Callable[[str, str, str], bool]) -> NormalizeResult:
    """Drop whole assignment lines, tracking the resource type they sit in.

    Only single-line scalar assignments are considered. A multi-line value would
    leave its continuation orphaned, and the attributes this needs to remove are
    all scalars.
    """
    out: List[str] = []
    dropped: List[str] = []
    current_type = ""
    depth = 0

    for line in hcl_text.splitlines():
        header = _RESOURCE_HEADER.match(line)
        if header:
            current_type = header.group("type")
            depth = 1
            out.append(line)
            continue

        if current_type:
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                current_type = ""

        match = _ASSIGNMENT.match(line)
        if match and current_type:
            value = match.group("value").strip()
            # Anything that opens a block or list continues onto later lines.
            if not value.endswith(("{", "[", "(")):
                if should_drop(current_type, match.group("attr"), value):
                    dropped.append(f"{current_type}.{match.group('attr')}")
                    continue

        out.append(line)

    trailing = "\n" if hcl_text.endswith("\n") else ""
    return NormalizeResult(text="\n".join(out) + trailing, dropped=dropped)


def drop_read_only(hcl_text: str, schema: Dict[str, Any]) -> NormalizeResult:
    by_type = read_only_attributes(schema)

    def should_drop(res_type: str, attr: str, _value: str) -> bool:
        return attr in by_type.get(res_type, set())

    result = _filter_lines(hcl_text, should_drop)
    if result.dropped:
        logger.info("dropped %d read-only attribute(s): %s", result.count, ", ".join(sorted(set(result.dropped))))
    return result


def drop_nulls(hcl_text: str) -> NormalizeResult:
    """Remove `attr = null`.

    Explicitly assigning null is equivalent to omitting the attribute, so these
    carry no meaning and only lengthen the diff.
    """
    result = _filter_lines(hcl_text, lambda _t, _a, value: value == "null")
    if result.dropped:
        logger.info("dropped %d null assignment(s)", result.count)
    return result


def drop_lines(hcl_text: str, line_numbers: Sequence[int]) -> NormalizeResult:
    """Remove specific 1-indexed lines, as reported by terraform validate."""
    targets = set(line_numbers)
    out: List[str] = []
    dropped: List[str] = []
    for number, line in enumerate(hcl_text.splitlines(), start=1):
        if number in targets:
            dropped.append(line.strip())
            continue
        out.append(line)
    trailing = "\n" if hcl_text.endswith("\n") else ""
    return NormalizeResult(text="\n".join(out) + trailing, dropped=dropped)


def invalid_lines(diagnostics: Sequence[Dict[str, Any]], filename: str) -> List[int]:
    """Line numbers terraform reported errors on, for one file."""
    lines: List[int] = []
    for diag in diagnostics:
        rng = diag.get("range") or {}
        if (rng.get("filename") or "").endswith(filename):
            start = (rng.get("start") or {}).get("line")
            if isinstance(start, int):
                lines.append(start)
    return sorted(set(lines))
