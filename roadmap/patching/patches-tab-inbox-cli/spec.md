---
title: "12.9 Patches tab, inbox, CLI"
paths:
  - "captains/patch/inbox.py"
  - "captains/patch/commands.py"
  - "compass_api/routes/**"
  - "dashboard/**"
criteria:
  - id: shipped
    text: "Findings by workload and repo with exposure, PR and verification; inbox kinds patch.approve / needs_you / deploy; compass patch and the MCP tools; one line in compass status."
    tests: [tests/api/test_patches.py]
---

Findings by workload and repo with exposure, PR and verification; inbox kinds patch.approve / needs_you / deploy; compass patch and the MCP tools; one line in compass status.

- Size: M, two to four weeks
- Shipped in 0.9.0 (PR #25)
- Design: [docs/specs/patching-captain.md](/docs/specs/patching-captain.md)
- Roadmap item 12.9
