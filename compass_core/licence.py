"""What a workspace may run (docs/captain_extensions_spec.md §2.7).

Tier and plan are different things: a manifest says which tier a captain
belongs to; the licence says which plan the workspace is on and, from now on,
which captains and tiers it names. The core decides from what it holds and
never calls anyone (D21).

The default resolver reads the workspace config's ``billing`` section, where
whoever activated the licence stored the verified claims: ``plan``,
``licensed_captains``, ``licensed_tiers``, ``license_expires_at``. The open
edition has no activation route, so it is on the free plan unless a host
installs a resolver with ``set_licence_resolver``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, FrozenSet, Mapping, Optional

FREE = "free"
CLOUD = "cloud"
ENTERPRISE = "enterprise"
PLANS = (FREE, CLOUD, ENTERPRISE)


@dataclass(frozen=True)
class Licence:
    plan: str = FREE
    captains: FrozenSet[str] = field(default_factory=frozenset)   # names, or "*"
    tiers: FrozenSet[str] = field(default_factory=frozenset)
    expires_at: Optional[str] = None                              # ISO 8601; None = no expiry

    @property
    def expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            when = datetime.fromisoformat(str(self.expires_at).replace("Z", "+00:00"))
        except ValueError:
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when <= datetime.now(timezone.utc)

    @property
    def effective_plan(self) -> str:
        plan = str(self.plan or FREE).lower()
        if plan not in PLANS or self.expired:
            return FREE
        return plan

    def is_licensed(self, manifest: Any) -> bool:
        """Open captains always; named captains and tiers; else the plan rule.

        The plan rule keeps today's behaviour: ``cloud``-tier captains are usable
        on the free plan (with the job limits the host enforces) and
        ``enterprise``-tier ones need the enterprise plan or a claim (D22)."""
        tier = getattr(manifest, "tier", "open")
        name = getattr(manifest, "name", "")
        if tier == "open":
            return True
        if not self.expired and (name in self.captains or "*" in self.captains or tier in self.tiers):
            return True
        plan = self.effective_plan
        if tier == "cloud":
            return plan in (FREE, CLOUD, ENTERPRISE)
        if tier == "enterprise":
            return plan == ENTERPRISE
        return False

    def to_dict(self) -> dict:
        return {
            "plan": self.effective_plan,
            "configured_plan": self.plan,
            "captains": sorted(self.captains),
            "tiers": sorted(self.tiers),
            "expires_at": self.expires_at,
            "expired": self.expired,
        }


def _names(value: Any) -> FrozenSet[str]:
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",")]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(v).strip() for v in value if str(v).strip())


def from_config(config: Optional[Mapping[str, Any]]) -> Licence:
    billing = (config or {}).get("billing") if isinstance(config, Mapping) else None
    if not isinstance(billing, Mapping):
        return Licence()
    return Licence(
        plan=str(billing.get("plan") or FREE).lower(),
        captains=_names(billing.get("licensed_captains")),
        tiers=_names(billing.get("licensed_tiers")),
        expires_at=str(billing["license_expires_at"]) if billing.get("license_expires_at") else None,
    )


Resolver = Callable[[Optional[Mapping[str, Any]]], Licence]
_resolver: Optional[Resolver] = None


def set_licence_resolver(resolver: Optional[Resolver]) -> None:
    """Install how a workspace's licence is found; ``None`` restores the config reader."""
    global _resolver
    _resolver = resolver


def licence_for(config: Optional[Mapping[str, Any]]) -> Licence:
    return (_resolver or from_config)(config)
