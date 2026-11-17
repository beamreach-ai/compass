---
title: "3.5 Write access only by PR, per whitelisted action"
paths:
  - "compass_core/grants.py"
  - "compass_core/access_pr.py"
  - "compass_core/actions.py"
criteria:
  - id: shipped
    text: "Compass only reads."
    tests: [tests/compass_core/test_grants.py]
---

Compass only reads. It proposes an action; the user accepts it (approve each time, or autopilot). Compass then opens the IaC PR that grants itself exactly that action. Removing it from the whitelist opens the PR that takes the permission away. Generalises the honeypot responder role to every captain.

- Size: M, two to four weeks
- Shipped in 0.7.0 (PR #17)
- Design: [docs/specs/access-model.md](/docs/specs/access-model.md)
- Roadmap item 3.5
