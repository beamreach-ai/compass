---
title: "3.6 One session broker for every captain"
paths:
  - "compass_core/sessions.py"
criteria:
  - id: shipped
    text: "Every AWS session, first-party captains included, comes from session_for(captain, job, capability): RoleSessionName compass-<captain>-<job>, SourceIdentity set to the approver, a session policy narrowed to what that captain or action declared."
    tests: [tests/compass_core/test_sessions.py, tests/compass_core/test_sessions_only.py]
---

Every AWS session, first-party captains included, comes from session_for(captain, job, capability): RoleSessionName compass-<captain>-<job>, SourceIdentity set to the approver, a session policy narrowed to what that captain or action declared. A CI test fails on boto3 used outside the broker.

- Size: S, under a week
- Shipped in 0.7.0 (PR #17)
- Design: [docs/specs/access-model.md](/docs/specs/access-model.md)
- Roadmap item 3.6
