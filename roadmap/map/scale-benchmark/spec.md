---
title: "1.10 Scale benchmark in CI"
paths:
  - "tests/benchmark/**"
  - "infra_map/synthetic.py"
criteria:
  - id: shipped
    text: "A synthetic 100k-resource world in e2e/fakes with time budgets for scan, query and dashboard load."
    tests: [tests/benchmark/test_benchmark.py]
---

A synthetic 100k-resource world in e2e/fakes with time budgets for scan, query and dashboard load.

- Size: S, under a week
- Shipped in 0.6.0 (PRs #12–#14)
- Design: [docs/specs/map-store.md](/docs/specs/map-store.md)
- Roadmap item 1.10
