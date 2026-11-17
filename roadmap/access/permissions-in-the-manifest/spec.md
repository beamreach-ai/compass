---
title: "3.4 Permissions in the manifest"
paths:
  - "compass_core/manifest.py"
  - "compass_core/captain_validate.py"
  - "captains/*/manifest.py"
criteria:
  - id: shipped
    text: "Each captain declares the AWS reads it needs and the write actions it may propose, plus GitHub scopes and egress."
    tests: [tests/test_captain_manifests.py]
---

Each captain declares the AWS reads it needs and the write actions it may propose, plus GitHub scopes and egress. Checked by compass validate, shown before install.

- Size: M, two to four weeks
- Shipped in 0.7.0 (PR #17)
- Design: [docs/specs/access-model.md](/docs/specs/access-model.md)
- Roadmap item 3.4
