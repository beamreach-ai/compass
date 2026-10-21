"""The librarian's manifest: the map's read API (docs/captain_extensions_spec.md D24)."""

from __future__ import annotations

from compass_core import __version__
from compass_core.captains import CaptainManifest, Permissions

MANIFEST = CaptainManifest(
    name="librarian",
    label="Librarian",
    summary="Answers questions about the map: which module owns a resource, where its Terraform lives, what is near it.",
    tier="open",
    feature="maps",
    routers=("compass_api.routes.librarian:router",),
    route_prefixes=("/api/librarian",),
    depends_on=("mapper",),
    permissions=Permissions(),   # reads the map, never AWS
    requires_core=">=0.1,<1",
    version=__version__,
)
