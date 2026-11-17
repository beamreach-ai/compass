---
title: "3.1 compass connect: the profile only sets up"
paths:
  - "compass_core/connect.py"
criteria:
  - id: shipped
    text: "Run at first setup when the account id is known, and again whenever a new account or cloud is connected."
    tests: [tests/compass_core/test_connect.py]
---

Run at first setup when the account id is known, and again whenever a new account or cloud is connected. Uses the operator's profile once to create the read role, its trust (control account only, external id, SetSourceIdentity) and the forwarding rule. Compass never uses that profile to reach the target: no ~/.aws mount, no local_iam mode. Open question: the runtime's base identity off AWS (Roles Anywhere or an assume-only IAM user). Open: the runtime's base identity off AWS (Roles Anywhere or an assume-only IAM user).

- Size: M, two to four weeks
- Shipped in 0.7.0 (PR #17)
- Design: [docs/specs/access-model.md](/docs/specs/access-model.md)
- Roadmap item 3.1
