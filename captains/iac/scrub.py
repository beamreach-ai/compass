"""Removes sensitive values from generated HCL before it is written anywhere.

`terraform plan -generate-config-out` writes attribute values as it reads them
from AWS. Some of those are secrets, and the repositories these pull requests
land in are frequently public. Nothing generated may reach disk, a commit, or a
PR body until it has been through here.

Which attributes are sensitive is not guessed: the provider declares it, and
`terraform providers schema -json` reports it. A name-based denylist is kept
only as a floor for the case where the schema could not be loaded, because
failing open on this is not acceptable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("iac-scrub")

SCRUB_MARKER = "# TODO: sensitive — set explicitly or source from a secret store"

# Applied when the provider schema is unavailable, and as a backstop alongside
# it. Substring matches on the attribute name.
_ALWAYS_SUSPECT = (
    "password",
    "secret",
    "token",
    "private_key",
    "certificate_body",
    "credential",
    "passphrase",
    "session_key",
    "shared_key",
    "auth",
)

_ASSIGNMENT = re.compile(r'^(?P<indent>\s*)(?P<attr>[A-Za-z_][A-Za-z0-9_-]*)(?P<eq>\s*=\s*)(?P<value>.+?)\s*$')
_RESOURCE_HEADER = re.compile(r'^\s*resource\s+"(?P<type>[^"]+)"\s+"[^"]+"\s*\{')


class ScrubError(RuntimeError):
    """Raised when scrubbing cannot be performed safely."""


@dataclass
class ScrubResult:
    text: str
    redacted: List[str] = field(default_factory=list)
    schema_used: bool = False

    @property
    def count(self) -> int:
        return len(self.redacted)


def sensitive_attributes(schema: Dict[str, Any]) -> Dict[str, Set[str]]:
    """Map resource type -> attribute names the provider marks sensitive."""
    out: Dict[str, Set[str]] = {}
    for provider in (schema.get("provider_schemas") or {}).values():
        for res_type, res_schema in (provider.get("resource_schemas") or {}).items():
            attrs = ((res_schema.get("block") or {}).get("attributes")) or {}
            flagged = {name for name, spec in attrs.items() if spec.get("sensitive")}
            if flagged:
                out.setdefault(res_type, set()).update(flagged)
    return out


def _is_sensitive(res_type: Optional[str], attr: str, by_type: Dict[str, Set[str]]) -> bool:
    if res_type and attr in by_type.get(res_type, set()):
        return True
    lowered = attr.lower()
    return any(token in lowered for token in _ALWAYS_SUSPECT)


def scrub(
    hcl_text: str,
    provider_schema: Optional[Dict[str, Any]] = None,
    *,
    require_schema: bool = False,
) -> ScrubResult:
    """Replace sensitive attribute values with a marker.

    The value is removed rather than masked. A masked value still tells a reader
    the length and shape of the secret, and a partially-redacted string in a
    public diff is not meaningfully better than the secret itself.

    `require_schema` makes a missing schema fatal. The pipeline sets it when the
    destination repository is public, where the name-based floor alone is not a
    responsible basis for publishing.
    """
    by_type: Dict[str, Set[str]] = {}
    schema_used = False
    if provider_schema:
        by_type = sensitive_attributes(provider_schema)
        schema_used = bool(by_type)

    if require_schema and not schema_used:
        raise ScrubError(
            "provider schema unavailable, so sensitive attributes cannot be identified "
            "authoritatively; refusing to publish generated configuration"
        )

    out: List[str] = []
    redacted: List[str] = []
    current_type: Optional[str] = None

    for line in hcl_text.splitlines():
        header = _RESOURCE_HEADER.match(line)
        if header:
            current_type = header.group("type")
            out.append(line)
            continue
        if line.strip() == "}":
            current_type = None
            out.append(line)
            continue

        match = _ASSIGNMENT.match(line)
        if not match:
            out.append(line)
            continue

        attr = match.group("attr")
        if not _is_sensitive(current_type, attr, by_type):
            out.append(line)
            continue

        # Drop the value entirely; keep the attribute name visible so a reviewer
        # knows something has to be supplied.
        out.append(f'{match.group("indent")}{SCRUB_MARKER}')
        out.append(f'{match.group("indent")}# {attr} = (redacted)')
        redacted.append(f"{current_type or '?'}.{attr}")

    if redacted:
        logger.warning("redacted %d sensitive value(s): %s", len(redacted), ", ".join(sorted(set(redacted))))

    trailing = "\n" if hcl_text.endswith("\n") else ""
    return ScrubResult(text="\n".join(out) + trailing, redacted=redacted, schema_used=schema_used)
