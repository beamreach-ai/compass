"""The map benchmark: a synthetic world of a chosen size and the measurements
docs/specs/map-store.md section 10 defines, run against each map store.

    .venv/bin/python -m tests.benchmark.run --scale 10k --store legacy,sqlite

`pytest -m benchmark` runs the 10k point with the budgets; `pytest -q` skips it.
"""
