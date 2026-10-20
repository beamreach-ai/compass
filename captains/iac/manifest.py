"""Code-as-cloud's manifest (docs/captain_extensions_spec.md §2.1).

Its routes live in the maps router today (``/api/iac/*`` beside ``/api/maps``);
the prefix is what makes them this captain's."""

from __future__ import annotations

from captains.mapper.permissions import INVENTORY_READS
from compass_core import __version__
from compass_core.captains import AwsPermissions, CaptainManifest, Permissions, TabSpec

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
    # terraform plan refreshes what it imports: the inventory's reads and the state bucket
    permissions=Permissions(aws=AwsPermissions(read=INVENTORY_READS), github=("contents:write", "pull_requests:write"), egress=("api.github.com",)),
    requires_core=">=0.1,<1",
    version=__version__,
)
