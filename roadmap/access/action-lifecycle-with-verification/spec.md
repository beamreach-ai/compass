---
title: "3.11 Action lifecycle with verification"
paths:
  - "compass_core/grants.py"
criteria:
  - id: shipped
    text: "Each whitelisted action moves proposed → PR open → applied → verified → armed."
    tests: [tests/compass_core/test_grants.py]
---

Each whitelisted action moves proposed → PR open → applied → verified → armed. Verified means iam:SimulatePrincipalPolicy confirms the grant is live. Removal disarms at once; the permission shows as pending removal until its PR merges.

- Size: S, under a week
- Shipped in 0.7.0 (PR #17)
- Design: [docs/specs/access-model.md](/docs/specs/access-model.md)
- Roadmap item 3.11
