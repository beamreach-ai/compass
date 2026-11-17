---
title: "12.4 Fix pull requests"
paths:
  - "captains/patch/fixes.py"
  - "captains/patch/pr.py"
  - "captains/patch/lockfiles.py"
criteria:
  - id: shipped
    text: "One PR per repo grouping every fix: FROM tag bumps, apt/apk pins, Bref layers, Lambda runtimes, AMIs in Terraform, manifest and lockfile bumps."
    tests: [tests/captains/patch/test_fixes.py, tests/captains/patch/test_pr.py, tests/captains/patch/test_lockfiles.py]
---

One PR per repo grouping every fix: FROM tag bumps, apt/apk pins, Bref layers, Lambda runtimes, AMIs in Terraform, manifest and lockfile bumps. Deterministic edits; the model writes only the explanation. The PR watcher keeps it alive until a person merges.

- Size: L, one to two months
- Shipped in 0.9.0 (PR #25)
- Design: [docs/specs/patching-captain.md](/docs/specs/patching-captain.md)
- Roadmap item 12.4
