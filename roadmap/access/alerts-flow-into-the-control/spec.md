---
title: "3.3 Alerts flow into the control account"
paths:
  - "compass_core/connect.py"
  - "captains/honeypot/**"
criteria:
  - id: shipped
    text: "Target accounts forward decoy events to a bus in the control account; init_alerts stops writing into the watched account."
    tests: [e2e/test_12_access.py]
---

Target accounts forward decoy events to a bus in the control account; init_alerts stops writing into the watched account.

- Size: M, two to four weeks
- Shipped in 0.7.0 (PR #17)
- Design: [docs/specs/access-model.md](/docs/specs/access-model.md)
- Roadmap item 3.3
