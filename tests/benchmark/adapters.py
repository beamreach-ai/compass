"""GraphStore adapters for the benchmark: added with the interface (docs/specs/map-store.md section 3)."""

from __future__ import annotations


def resolve(name: str):
    raise LookupError(f"no benchmark store named {name!r}; the GraphStore backends are not built yet (only legacy)")
