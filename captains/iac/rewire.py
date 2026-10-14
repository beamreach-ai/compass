"""Rewrites literal values in generated HCL into references.

`terraform plan -generate-config-out` is accurate but flat: every value is a
literal, because generation reads live AWS and knows nothing about the
configuration it will live beside.

    resource "aws_security_group" "public_demo_demo_alb" {
      vpc_id      = "vpc-0e040db1f49390291"
      description = "ALB security group"
    }

A human writing the same block would have written
`vpc_id = module.beamreach-demo-vpc.vpc_id`. This module performs that
substitution, using the index built from the target directory.

The pass is deliberately deterministic rather than model-driven. Facts come from
terraform, so the rewrite can be checked: the verify stage re-plans and requires
that nothing changed. A model choosing substitutions could not be checked the
same way. Judgement is reserved for what has no ground truth — ambiguity,
placement, idiom — and applied afterwards.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Set

from captains.iac.reference_index import ReferenceIndex

logger = logging.getLogger("iac-rewire")

# Attributes that define what a resource *is*, rather than what it points at.
# Rewriting these makes the diff unreadable and can make the import ambiguous —
# a security group whose `name` became a reference no longer obviously matches
# the thing being imported.
_IDENTITY_ATTRS = {"name", "name_prefix", "bucket", "function_name", "table_name", "identifier"}

# `attribute = "value"` at any indentation. Multi-line and heredoc values are
# left alone: whole-value matching cannot apply to them and attempting a partial
# rewrite inside a heredoc risks corrupting it.
_ASSIGNMENT = re.compile(r'^(?P<indent>\s*)(?P<attr>[A-Za-z_][A-Za-z0-9_-]*)(?P<eq>\s*=\s*)"(?P<value>[^"\\]*)"\s*$')

_RESOURCE_HEADER = re.compile(r'^\s*(resource|data)\s+"(?P<type>[^"]+)"\s+"(?P<name>[^"]+)"\s*\{')


@dataclass
class Substitution:
    line_number: int
    attribute: str
    literal: str
    expression: str
    alternatives: List[str] = field(default_factory=list)


@dataclass
class RewireResult:
    text: str
    substitutions: List[Substitution] = field(default_factory=list)
    skipped_identity: List[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.substitutions)

    def report_lines(self) -> List[str]:
        """Human-readable summary for the PR body.

        Ambiguous choices are listed explicitly: the pass picked one, and a
        reviewer should be able to see what else it could have picked without
        reading the index.
        """
        lines: List[str] = []
        for sub in self.substitutions:
            entry = f"- `{sub.attribute}`: `\"{sub.literal}\"` → `{sub.expression}`"
            if sub.alternatives:
                others = ", ".join(f"`{a}`" for a in sorted(set(sub.alternatives)) if a != sub.expression)
                if others:
                    entry += f" (also matched {others})"
            lines.append(entry)
        return lines


def rewire(
    hcl_text: str,
    index: ReferenceIndex,
    *,
    protected_values: Sequence[str] = (),
    identity_attrs: Optional[Set[str]] = None,
) -> RewireResult:
    """Replace literals with references, whole-value matches only.

    `protected_values` are the import IDs of the resources in this batch. A
    generated block frequently contains the very id it is being imported by;
    rewriting that to a reference would make the config describe a different
    resource than the import block claims.

    Partial and interpolated matches (an account id embedded in an ARN) are not
    attempted here. They read better but are materially riskier, and belong
    behind a flag once the whole-value path is proven.
    """
    identity = _IDENTITY_ATTRS if identity_attrs is None else identity_attrs
    protected = {v for v in protected_values if v}

    out_lines: List[str] = []
    subs: List[Substitution] = []
    skipped: List[str] = []

    for number, line in enumerate(hcl_text.splitlines(), start=1):
        match = _ASSIGNMENT.match(line)
        if not match:
            out_lines.append(line)
            continue

        attr = match.group("attr")
        literal = match.group("value")

        if literal in protected:
            out_lines.append(line)
            continue

        if attr in identity:
            if index.lookup(literal) is not None:
                skipped.append(f"{attr}={literal}")
            out_lines.append(line)
            continue

        reference = index.lookup(literal)
        if reference is None:
            out_lines.append(line)
            continue

        out_lines.append(f"{match.group('indent')}{attr}{match.group('eq')}{reference.expression}")
        subs.append(
            Substitution(
                line_number=number,
                attribute=attr,
                literal=literal,
                expression=reference.expression,
                alternatives=index.alternatives(literal),
            )
        )

    trailing = "\n" if hcl_text.endswith("\n") else ""
    return RewireResult(text="\n".join(out_lines) + trailing, substitutions=subs, skipped_identity=skipped)


def revert_attribute(hcl_text: str, attribute: str, literal: str) -> str:
    """Put one attribute back to its literal.

    Used when the verify stage reports a dependency cycle: rewiring created an
    edge terraform cannot order, so that single substitution is undone and the
    plan is retried rather than abandoning the whole rewrite.
    """
    # The value guard is positional rather than a lookahead: `(?!")` placed after
    # a greedy `\s*` can be satisfied by backtracking the whitespace, which made
    # this rewrite already-literal attributes and eat their spacing. Requiring
    # the value to *begin* with a non-quote character cannot backtrack that way.
    pattern = re.compile(
        rf'^(?P<indent>[ \t]*){re.escape(attribute)}(?P<eq>[ \t]*=[ \t]*)(?P<value>[^"\s][^\n]*)$',
        re.MULTILINE,
    )

    def _replace(m: re.Match) -> str:
        return f'{m.group("indent")}{attribute}{m.group("eq")}"{literal}"'

    return pattern.sub(_replace, hcl_text, count=1)


def resource_addresses(hcl_text: str) -> List[str]:
    """Addresses declared in a generated file, for excluding self-references."""
    found: List[str] = []
    for line in hcl_text.splitlines():
        match = _RESOURCE_HEADER.match(line)
        if match:
            prefix = "data." if line.lstrip().startswith("data") else ""
            found.append(f"{prefix}{match.group('type')}.{match.group('name')}")
    return found
