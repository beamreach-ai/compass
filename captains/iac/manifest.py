"""Code-as-cloud's manifest (docs/captain_extensions_spec.md §2.1).

Its routes live in the maps router today (``/api/iac/*`` beside ``/api/maps``);
the prefix is what makes them this captain's."""

from __future__ import annotations

from compass_core import __version__
from compass_core.captains import CaptainManifest, TabSpec

MANIFEST = CaptainManifest(
    name="iac",
    label="Code-as-cloud",
    summary="Imports unmanaged resources into Terraform and fixes drift between the state and the account.",
    tier="open",
    feature="iac",
    commands="captains.iac.commands",
    route_prefixes=("/api/iac",),
    tabs=(TabSpec(key="iac", label="IAC", group="cost", feature="iac"),),
    queue="iac-executions",
    depends_on=("mapper",),
    requires_core=">=0.1,<1",
    version=__version__,
)
