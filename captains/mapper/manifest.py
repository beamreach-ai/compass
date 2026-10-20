"""The infra map's manifest (docs/captain_extensions_spec.md §2.1).

The mapper has no section of its own: it reads the AWS and GitHub settings
(regions, repositories, limits) every captain shares."""

from __future__ import annotations

from captains.mapper.permissions import PERMISSIONS
from compass_core import __version__
from compass_core.captains import CaptainManifest, ServiceSpec, TabSpec

MANIFEST = CaptainManifest(
    name="mapper",
    label="Infra map",
    summary="Maps the AWS account, the Terraform state and the repositories into one graph the other captains read.",
    tier="open",
    feature="maps",
    commands="captains.mapper.commands",
    routers=("compass_api.routes.maps:router",),
    route_prefixes=("/api/maps",),
    tabs=(TabSpec(key="maps", label="Maps", group="platform", feature="maps"),),
    queue="mapper-executions",
    services=(ServiceSpec(name="map-rescan", start="captains.mapper.service:run_service", interval=0, interval_setting="map_interval"),),
    permissions=PERMISSIONS,
    requires_core=">=0.1,<1",
    version=__version__,
)
