"""The toy captain's manifest."""

from __future__ import annotations

from compass_core.captains import CaptainManifest, TabSpec

MANIFEST = CaptainManifest(
    name="hello",
    label="Hello",
    summary="Says hello. The example in docs/writing_a_captain.md.",
    tier="open",
    feature="hello",
    commands="tests.fixtures.captain_hello.commands",
    routers=("tests.fixtures.captain_hello.routes:router",),
    route_prefixes=("/api/hello",),
    tabs=(TabSpec(key="hello", label="Hello", group="platform", feature="hello"),),
    config_schema={
        "type": "object",
        "properties": {
            "greeting": {"type": "string", "default": "hello", "x-help": "What to say."},
            "api_key": {"type": "string", "x-secret": True},
        },
    },
    requires_core=">=0.1,<1",
    version="0.0.1",
)
