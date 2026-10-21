"""Run the map benchmark from the shell and print the numbers against the budgets.

    .venv/bin/python -m tests.benchmark.run --scale 10k --store legacy,sqlite,postgres --out bench.json

Stores: legacy (today's DynamoDB code), and the GraphStore backends. Endpoints:
BENCH_DYNAMODB_URL (default http://localhost:18000), BENCH_POSTGRES_DSN, BENCH_SQLITE_PATH.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List

from infra_map.synthetic import WorldSpec, churn, describe, generate
from tests.benchmark.budgets import BUDGETS, ENFORCED, REPORTED_ONLY, SCALES, budget
from tests.benchmark.harness import measure, resolve_adapter


def fmt(metric: str, value: Any) -> str:
    if value is None:
        return "-"
    if metric.endswith("_bytes"):
        return f"{value / 1e6:.2f} MB" if value >= 1e6 else f"{value / 1e3:.0f} KB"
    if metric.endswith("_ms"):
        return f"{value:.0f} ms"
    if metric.endswith("_s"):
        return f"{value:.1f} s"
    if metric.endswith("_mb"):
        return f"{value:.0f} MB"
    return str(value)


def table(results: List[Dict[str, Any]], scale: str) -> str:
    stores = [r["store"] for r in results]
    lines = ["| metric | budget | " + " | ".join(stores) + " |", "|---|---|" + "---|" * len(stores)]
    for metric in BUDGETS:
        cells = []
        for r in results:
            value = r.get(metric)
            b = budget(metric, scale)
            flag = "" if value is None or b is None or value <= b else " **over**"
            cells.append(fmt(metric, value) + flag)
        lines.append(f"| {metric} | {fmt(metric, budget(metric, scale))} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def failures(result: Dict[str, Any], scale: str) -> List[str]:
    if scale not in ENFORCED:
        return []
    out = []
    for metric in BUDGETS:
        if metric in REPORTED_ONLY:
            continue
        value, b = result.get(metric), budget(metric, scale)
        if value is not None and b is not None and value > b:
            out.append(f"{result['store']}: {metric} {fmt(metric, value)} over {fmt(metric, b)}")
    return out


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scale", default="10k", choices=sorted(SCALES))
    parser.add_argument("--store", default="legacy", help="comma-separated adapter names")
    parser.add_argument("--out", help="write the results as JSON here")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--regions", type=int, default=4)
    parser.add_argument("--fail-on-budget", action="store_true", help="exit 1 when an enforced budget is exceeded")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    spec = WorldSpec(resources=SCALES[args.scale], regions=args.regions, seed=args.seed)
    world = generate(spec)
    churned = churn(world)
    print(f"world: {describe(world)}", flush=True)
    print(f"churned: {describe(churned)} (generated in {time.perf_counter() - started:.1f}s)", flush=True)

    results: List[Dict[str, Any]] = []
    for name in [s.strip() for s in args.store.split(",") if s.strip()]:
        adapter = resolve_adapter(name)
        results.append(measure(adapter, world, churned, log=lambda line: print(line, flush=True)))

    print()
    print(f"## {args.scale}: {world.node_count} nodes, {world.edge_count} edges")
    print(table(results, args.scale))
    failed = [f for r in results for f in failures(r, args.scale)]
    if failed:
        print("\nover budget:\n- " + "\n- ".join(failed))
    if args.out:
        with open(args.out, "w") as handle:
            json.dump({"scale": args.scale, "world": describe(world), "results": results}, handle, indent=2, default=str)
    return 1 if (failed and args.fail_on_budget) else 0


if __name__ == "__main__":
    sys.exit(main())
