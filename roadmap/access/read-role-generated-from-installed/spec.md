---
title: "3.2 Read role generated from installed captains"
paths:
  - "compass_core/access_module.py"
  - "compass_core/permissions.py"
  - "captains/*/permissions.py"
criteria:
  - id: shipped
    text: "The read policy is the union of the installed captains' declared reads, the mapper's included."
    tests: [tests/captains/test_permissions.py]
---

The read policy is the union of the installed captains' declared reads, the mapper's included. Installing or removing a captain opens a PR that changes it.

- Size: S, under a week
- Shipped in 0.7.0 (PR #17)
- Design: [docs/specs/access-model.md](/docs/specs/access-model.md)
- Roadmap item 3.2
