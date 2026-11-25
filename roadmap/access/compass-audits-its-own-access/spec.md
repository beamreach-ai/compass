---
title: "3.12 Compass audits its own access"
paths:
  - "compass_core/access_audit.py"
criteria:
  - id: shipped
    text: "Compass reads its own roles and flags any permission no whitelist entry or installed captain explains, such as one added in the console."
    tests: [tests/compass_core/test_access_audit.py]
---

Compass reads its own roles and flags any permission no whitelist entry or installed captain explains, such as one added in the console. It opens a PR to remove it.

- Size: S, under a week
- Shipped in 0.7.0 (PR #17)
- Design: [docs/specs/access-model.md](/docs/specs/access-model.md)
- Roadmap item 3.12
