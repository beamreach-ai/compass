---
name: compass-patch
description: Walk the user through the CVEs that reach their account and the fix pull requests Compass opens for them. Use when the user asks about a CVE, a vulnerability, a disclosure ("is php2shell on us?"), what is patched, what is waiting, or wants a fix pull request opened, a finding ignored, or the feeds run now.
---

# Patches

What Compass does on its own: it follows the CVE feeds every hour, matches them
against the software on the map every fifteen minutes, and opens one fix pull
request per repository for findings that are exploited (KEV), likely to be
(EPSS), severe (CVSS) or on a workload that faces the internet. Everything else
waits in the inbox for a person. Nothing merges: the user does.

1. `compass_patch_findings`. Lead with the exposed and exploited findings, then
   what is waiting for merge (`pr_open` with its pull request URL and the
   verification line: CI, build, canary), then what waits for the user (verdict
   `inbox`). Say the fixed version for each. A finding with a `note` has
   something in the way of a plain bump (a floating tag, a transitive
   dependency, a major upgrade, no fix yet): say it in those words.
2. "Is CVE-X on us?": `compass_patch_findings` filtered; if the feeds have not
   run since the disclosure (`feed.last_run`), `compass_patch_feed run`, then
   `tick`, then look again. The feeds are hourly; a disclosure of the last hour
   may not be in yet.
3. To open a pull request the policy left to a person: show the findings, get
   the user's yes, then `compass_patch_pr <repo> approve=true`. The inbox has
   the same item (`compass_inbox`, kind `patch.approve`).
4. To silence a finding: `compass_patch_ignore <key> until=… reason=…` only when
   the user gives the reason; say it will come back on that date.
5. A `needs_you` on a fix pull request (`compass_prs`, reason `canary_failed`
   or `human_commit`): read the pull request's last comment; the canary's
   timeline says which probe failed and that the staging twin was rolled back.
6. Never say a site is covered when it is not on the map: an EC2 host without
   SSM, anything outside AWS, a repository the mapper does not read. The
   `coverage` line says how many images were resolved.
