"""The example captain's manifest: the smallest complete captain (docs/writing_a_captain.md)."""

from __future__ import annotations

from compass_core.captains import CaptainManifest, TabSpec

__version__ = "0.1.0"

MANIFEST = CaptainManifest(
    name="hello",
    label="Hello",
    summary="Says hello. The example captain: copy it to start your own.",
    tier="open",
    feature="hello",
    commands="captains.hello.commands",
    routers=("captains.hello.routes:router",),
    route_prefixes=("/api/hello",),
    tabs=(TabSpec(key="hello", label="Hello", group="platform", feature="hello"),),
    config_schema={
        "type": "object",
        "properties": {
            "greeting": {"type": "string", "default": "hello", "x-help": "What to say."},
            "api_key": {"type": "string", "x-secret": True},
        },
    },
    requires_core=">=0.2,<1",
    version=__version__,
)
