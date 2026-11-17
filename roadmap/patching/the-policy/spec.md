---
title: "12.5 The policy"
paths:
  - "captains/patch/policy.py"
criteria:
  - id: shipped
    text: "A PR without a click for KEV, EPSS ≥ 0.1, CVSS ≥ 7 or an exposed workload; an inbox item to approve otherwise."
    tests: [e2e/test_15_patch.py]
---

A PR without a click for KEV, EPSS ≥ 0.1, CVSS ≥ 7 or an exposed workload; an inbox item to approve otherwise. No mode of its own: the watcher's policy and the grants are the controls.

- Size: S, under a week
- Shipped in 0.9.0 (PR #25)
- Design: [docs/specs/patching-captain.md](/docs/specs/patching-captain.md)
- Roadmap item 12.5
