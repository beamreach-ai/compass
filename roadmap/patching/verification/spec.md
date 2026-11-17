---
title: "12.6 Verification"
paths:
  - "captains/patch/verify.py"
criteria:
  - id: shipped
    text: "CI on the PR, an image build in the repo's own workflow (added when missing), and a canary: the fix's image on a named staging twin as a granted action, probed, compared, rolled back."
    tests: [tests/captains/patch/test_verify.py]
---

CI on the PR, an image build in the repo's own workflow (added when missing), and a canary: the fix's image on a named staging twin as a granted action, probed, compared, rolled back. Ready means all three that applied passed.

- Size: L, one to two months
- Shipped in 0.9.0 (PR #25)
- Design: [docs/specs/patching-captain.md](/docs/specs/patching-captain.md)
- Roadmap item 12.6
