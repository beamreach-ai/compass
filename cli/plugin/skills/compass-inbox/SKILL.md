---
name: compass-inbox
description: Triage what needs a person in Compass — parked decoy responses, grants, pull requests waiting on a human, failed jobs, patch findings — and act on items with the user's go. Use when the user asks "what needs me", "anything waiting", "what should I approve", or asks to act on something Compass parked.
---

# The inbox

1. `compass_inbox`. Read it whole before acting. Lead with the urgent items,
   then by verb: what asks for a decision (`approve`), what needs a repair
   (`fix`), what waits for a merge on GitHub (`merge`), what can wait (`later`).
2. For each item the user asks about, show the `detail` the way the tab would:
   a parked response's risk breakdown and the ARNs it touches, a grant's IAM
   statements, a pull request's reviewer comments, a job's error and last
   events, a patch item's findings with their fixed versions.
3. Act only with the user's go, and only through `compass_inbox_act`: without
   `confirm` it returns the item's own confirm sentence; show that sentence,
   get the yes, then call again with `confirm: true`. Never act on an
   `approve` item without showing the sentence first. `merge` items are done
   on GitHub by the user, not by you.
4. `fix` items that are a repair of the install (no read role, GitHub not
   connected, no alert queue) lead to Settings; say which command fixes it
   (`compass account connect`, `compass github`).
5. Snooze only what has a natural end (`compass_inbox_snooze`): a failed job
   the user has looked at, an update until the next release, "no model on
   purpose". Nothing else can be hidden; it is run, declined, accepted or
   fixed.
6. The list is derived from state, never stored: an item that left between two
   calls was acted on or its state moved; say so instead of searching for it.
