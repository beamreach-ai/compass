"""The map benchmark as a test: the 10k point against the SQLite store and the budgets
of docs/specs/map-store.md. Skipped by default (`-m "not benchmark"` in pyproject);
`pytest -m benchmark` runs it, CI runs it for changes to the map code."""

from __future__ import annotations

import pytest

from tests.benchmark.harness import measure
from tests.benchmark.run import failures, table
from tests.benchmark.world import WorldSpec, churn, generate

pytestmark = pytest.mark.benchmark


def test_sqlite_meets_the_10k_budgets(tmp_path, monkeypatch):
    from tests.benchmark.adapters import resolve

    monkeypatch.setenv("BENCH_SQLITE_PATH", str(tmp_path / "bench.sqlite"))
    world = generate(WorldSpec(resources=10_000, regions=4))
    result = measure(resolve("sqlite"), world, churn(world), log=lambda _line: None)
    print(table([result], "10k"))
    over = failures(result, "10k")
    assert not over, "\n".join(over)
