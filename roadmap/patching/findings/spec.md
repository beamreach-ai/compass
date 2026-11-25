---
title: "12.3 Findings"
paths:
  - "captains/patch/findings.py"
  - "captains/patch/match.py"
  - "captains/patch/store.py"
criteria:
  - id: shipped
    text: "Advisory × software × node, with the fixed version, the owning repo and file, exposure, KEV/EPSS/CVSS; states open → pr_open → merged → deployed; closes itself when the inventory changes."
    tests: [tests/captains/patch/test_match.py]
---

Advisory × software × node, with the fixed version, the owning repo and file, exposure, KEV/EPSS/CVSS; states open → pr_open → merged → deployed; closes itself when the inventory changes.

- Size: M, two to four weeks
- Shipped in 0.9.0 (PR #25)
- Design: [docs/specs/patching-captain.md](/docs/specs/patching-captain.md)
- Roadmap item 12.3
