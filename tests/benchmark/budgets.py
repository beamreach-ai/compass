"""The budgets of docs/specs/map-store.md section 10.3: a measurement over its budget
fails the benchmark at 10k and 100k; 1M is reported only."""

from __future__ import annotations

from typing import Dict, Optional

SCALES = {"10k": 10_000, "100k": 100_000, "1m": 1_000_000}

# metric -> {scale: budget}; seconds unless the name says ms, MB or bytes
BUDGETS: Dict[str, Dict[str, float]] = {
    "scan_first_s": {"10k": 10, "100k": 60, "1m": 900},
    "scan_second_s": {"10k": 10, "100k": 60, "1m": 900},
    "first_load_s": {"10k": 1.0, "100k": 2.0, "1m": 3.0},
    "first_load_bytes": {"10k": 8_000_000, "100k": 100_000, "1m": 100_000},
    "neighborhood_hub_ms": {"10k": 100, "100k": 300, "1m": 1000},
    # a median node's depth-2 neighbourhood runs through a hub and hits the 500-node cap: the hub's budget
    "neighborhood_ms": {"10k": 100, "100k": 300, "1m": 1000},
    "page_ms": {"10k": 50, "100k": 100, "1m": 300},
    "summary_ms": {"10k": 100, "100k": 500, "1m": 3000},
    "ownership_ms": {"10k": 50, "100k": 100, "1m": 300},
    "paths_ms": {"10k": 100, "100k": 300, "1m": 1000},
    # the benchmark process: two worlds as dataclasses plus the resolver's whole-graph copy, not the store
    "peak_rss_mb": {"10k": 400, "100k": 2048, "1m": 8192},
    # SQLite with its nine indexes: about 2.9 KB a node and 1.1 KB an edge
    "db_size_mb": {"10k": 60, "100k": 500, "1m": 5000},
}
ENFORCED = ("10k", "100k")
# reported, not enforced: today's resolver loads the whole graph on every store until the
# query API moves it to paths (docs/specs/map-store.md section 14, PR 5)
REPORTED_ONLY = ("ownership_ms",)


def budget(metric: str, scale: str) -> Optional[float]:
    return BUDGETS.get(metric, {}).get(scale)
