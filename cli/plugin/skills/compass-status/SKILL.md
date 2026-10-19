---
name: compass-status
description: Brief the user on the state of their Compass install, what is new since last time and what needs attention. Use when the user asks "what's new", "how is Compass doing", for a morning brief, or at the start of a session before other Compass work.
---

# Status brief

1. `compass_status`. Lead with anything that needs attention: failed jobs, limit reached, unresolved honeytoken alerts, infra scan not complete, GitHub not connected, and a Compass release that is out (`update.available`: say the version, the headlines in `update.release_notes`, and where to apply it; never apply it yourself).
2. Then the numbers: findings by severity and domain, FinOps savings, open PRs, honeytokens armed.
3. If `jobs.recent_failures` is non-empty, `compass_jobs get` on the first and explain the error.
4. If `honeypots.active_alerts` > 0, `compass_honeypots incidents` and offer `compass_honeypots_incident summarize`.
5. Suggest one next action, not a list.
6. A tool that answers `captain_disabled` or `dependency_off` means that captain is turned off for the workspace: `compass_captains list` shows every captain and why it is off; `compass_captains enable <name>` (with `with_dependencies: true` when it names what it needs) turns it on, after the user agrees. `captain_unlicensed` is not something to work around: say which captain needs a licence.
