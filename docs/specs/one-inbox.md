# One inbox: everything that needs a person, counted on every tab, one feed

Status: draft 2026-10-22, for review. Roadmap item 4.7 (angle 4).
Line references are to `main` at c259dd5.

## 1. Goals and non-goals

**Goals.**

- A person opening Compass knows in one glance whether anything waits for
  them, and where: a count on every tab that has something, and a total.
- One feed, newest first, where every item can be read and, where one click
  is enough, acted on in place: run the parked response, accept the grant,
  retry the job, apply the update. Expanding an item shows what the tab
  would have shown before the same click.
- Each tab keeps a "Needs you" view of its own items, and the same feed
  exists for everything at once.
- The list is derived from state, never stored. An item appears when the
  state says a person is needed and leaves when the state changes. Nothing
  is marked read; nothing can go out of sync.
- The CLI and the MCP server see the same list, so an agent in Claude Code
  can answer "what needs me?" and act.
- Captains, including a product's, contribute items through one seam, as
  they contribute status sections today.

**Non-goals (for now).**

- A Slack or email digest of open items. Each kind already notifies when it
  happens; a daily digest from this list is a follow-up (section 3.9).
- Per-person inboxes. The open edition has one admin and API tokens; the
  product has users. Items are the workspace's; "by you" is decoration.
- Dismissing anything. A snooze exists only where the kind has a natural
  end (section 3.8).
- Open incidents with nothing pending. They are the Decoys tab's business
  (decision taken; section 9).
- Fixing the jobs table's full scan (8.1). The inbox reads the newest jobs
  through today's `list_jobs`; section 7 says what that costs.

## 2. Current state

**"Needs you" exists for one captain.** The PR watcher marks a pull request
`needs_human` with a reason (`captains/prs/captain.py:39`, `:227-236`),
labels it `compass:needs-human` on GitHub (`sync.py:26`), notifies
(`notify.py:36`, severity `high`), and the Pull requests tab has a
`needs_you` sub-view (`captains/prs/manifest.py:54`) that filters tracked
PRs whose watch status is `needs_human` or that carry a `pending_revision`
(`dashboard/app/components/PRsTab.tsx:133`), with the count in the view's
label (`:155`). The e2e suite covers it (`e2e/test_08_prs.py:43`).

**Everything else is a counter, a banner, or nothing.**

- A decoy response the gate parked (`mode: approve`, or risk above
  `risk_max`) sits in `incident.action.pending[]` with its risk and reason
  (`captains/honeypot/respond.py:103-121`); the incident panel shows an
  "Approve and run" button per entry (`HoneypotIncidents.tsx:479-485`) that
  calls `POST /api/honeypots/incidents/{id}/approve`
  (`honeypot_incidents.py:147-163`). The Decoys overview says "needs
  attention" when incidents are open (`HoneypotsTab.tsx:2100`). No count
  outside the tab.
- A grant waits in `proposed` for a person to accept or decline
  (`compass_core/grants.py:45-49`, `compass_api/routes/access.py:116-138`),
  then in `pr_open` for a merge on GitHub. Shown on Settings → Account →
  Access (`AccessSection.tsx:56`), nowhere else.
- The self-audit's findings (`unexplained`, `missing`, `not_under_code`;
  `compass_core/access_audit.py:103-119`) are listed on the same section
  (`AccessSection.tsx:143-145`); their fix is the access pull request
  (`access.py:169`).
- A failed job shows its error on the Queue tab and can be restarted
  (`compass_api/routes/jobs.py:101-109`; `e2e/test_09_jobs.py:35`); a job
  with no heartbeat for three minutes is marked dead (`compass_core/jobs.py:627`).
- An available update is a banner, dismissable until the next release
  (`CompassUpdates.tsx:136-167`), from `update` in the settings bootstrap
  (`settings.py:335`); applying is `POST /api/updates/apply`
  (`routes/updates.py:40`) when the updater runs.
- An incomplete setup (no connected account, no GitHub) is a warning on
  Settings (`DashboardClient.tsx:611`, `e2e/test_03_settings.py:39`).
- Decoys waiting on their own pull request (`pr_open`) and, once merged, on
  `arm`, are rows on the honeytokens list.

**The chrome.** The sidebar is the whole navigation on desktop; there is no
header bar. It polls `/api/settings/bootstrap`-derived metadata every 15 s
and refreshes on the `beamreach:target-updated` and `beamreach:queue-updated`
window events (`Sidebar.tsx:216-224`). A tab's sub-views come from its
manifest's `TabSpec.views`, labelled by `viewLabel` (`lib/tabs.ts:63-66`) and
linked by `viewHref`; Settings is one page whose views scroll to headings
(`DashboardClient.tsx:489-498`). On phones the sidebar is a drawer behind a
menu button in a top bar (`WorkspaceShell.tsx:41-55`).

**The seams.** The product adds status sections and bootstrap sections by
name (`compass_api/extensions.py:140-153`); `GET /api/status` is the one
call the CLI and an agent make first (`routes/status.py:162`,
`cli/src/tools/status.ts:1-12`). Adding a section is not an API version bump
(`compass_api/version.py:8-10`).

## 3. The design

```
sources (per request, cached 5 s)                 one list                      three surfaces
 incidents.pending  ─┐                                                           ┌─ sidebar counts: per tab + total
 grants.proposed     │                                                           │  (mobile: on the menu button)
 grants.pr_open      │   compass_core.inbox.items(config)  ──▶  [item, item …]  ─┼─ a tab's "Needs you" view (filtered)
 decoys.pr_open/arm  ├─▶ kind → verb, severity, since,          newest first     │  and the whole feed
 prs.needs_human     │   title, summary, detail, actions                         └─ compass inbox · compass_inbox (MCP)
 prs.pending_revision│                                                              compass status: one line
 jobs.failed/dead    │
 access.findings     │   actions are the routes the tabs already call;
 updates.available   │   the item carries them, so no surface knows the kinds
 setup.incomplete   ─┘
 + register_inbox_source(name, fn)   ← a product captain's items
```

### 3.1 Vocabulary

- **Item** — one thing a person is needed for, derived from state. It has a
  deterministic id (`<kind>:<subject id>`), so the client can tell new from
  known across polls without any server-side memory.
- **Kind** — which rule produced it (section 3.3). **Source** — the captain or
  core module whose state it reads.
- **Verb** — what is asked: `approve` (a decision), `fix` (a repair),
  `merge` (a merge on GitHub), `later` (a chore). The feed is not grouped by
  verb (decision taken); the verb is a chip on the item and a filter.
- **Severity** — `urgent`, `normal`, `info`: the stripe. Nothing else.
- **Since** — when the state began (parked at, proposed at, failed at, first
  seen): the feed's sort key, newest first.
- **The feed** — the list, in a tab's "Needs you" view filtered to that tab,
  or whole.
- **Snooze** — hiding an item until a condition the kind defines (section
  3.8). The only hiding there is.

### 3.2 The item

```json
{
  "id": "decoy.approval:7f3a…:ecs_restart_task",
  "kind": "decoy.approval",
  "verb": "approve",
  "severity": "urgent",
  "captain": "honeypot",
  "tab": "honeypots",
  "title": "Restart the task that leaked the key on incident 7f3a",
  "summary": "risk 2 · chosen by the agent · parked: mode is approve",
  "since": 1792670000000,
  "subject": {"incident_id": "7f3a…", "action_id": "…"},
  "actions": [
    {"key": "run", "label": "Run", "method": "POST",
     "path": "/api/honeypots/incidents/7f3a…/approve", "body": {"action_id": "…"},
     "scope": "operate", "confirm": "Runs ecs_restart_task on payments-api (risk 2)."},
    {"key": "open", "label": "Open incident", "href": "/workspace?tab=honeypots&view=history&incident=7f3a…"}
  ],
  "detail": {"risk": {"score": 2, "breakdown": {…}, "notes": […]}, "reason": "mode is approve",
             "context": {"task_arn": "…", "service_arn": "…", "user_arn": "…"},
             "incident": {"opened_at": "…", "honeytoken": "svc_nightly_backup", "source_ip": "203.0.113.9"}},
  "snooze": null
}
```

`actions` carry the method, path and body of the route the tab already
calls, plus the scope the token needs and the one-line confirmation the tab
shows. The dashboard, the CLI and the MCP tool render actions from this
list and know nothing about kinds; adding a kind adds nothing to them.
`href` actions are links. `detail` is what the tab shows before the same
click, as data, never markup. Nothing in an item is a secret: a token's
material, a key, a DSN never appear (the detail of `setup.incomplete`
names what is missing, not what is there).

### 3.3 The kinds

| Kind | Derived from | Verb · severity | Since | Actions | Detail | Leaves when |
|---|---|---|---|---|---|---|
| `decoy.approval` | each `action.pending[]` entry on an open incident | approve · urgent (risk ≥ 3), normal otherwise | the incident's `action_started_at`, else its open time | Run (`…/approve`); Decline (new: `…/decline`, records "declined by" and drops the entry); Open incident | risk breakdown and notes, reason, the context ARNs, the incident's token, source IP, opened at | run, declined, or the incident closed |
| `grant.proposed` | `/api/access` grants in `proposed` | approve · normal | `created_at` | Accept (`…/accept`); Decline (`…/decline`); Open access | the IAM statements, scope, change type, rollback, proposed by, what the access PR will contain | accepted or declined |
| `grant.waiting` | grants in `pr_open`, `revoke_pr_open` | merge · info | the PR's opened time | Open pull request (href); Verify now | the PR, the branch, the statements it adds or removes | merged (the grant moves on) |
| `decoy.waiting` | honeytokens whose plant or decommission PR is open | merge · info | the PR's opened time | Open pull request (href) | class, placement, the module path | merged |
| `decoy.arm` | honeytokens whose PR merged and that are not `armed` (material `pending_apply` resolved or not) | fix · normal | the merge time | Arm (`…/arm`); Open token | the arm checks' last result (what is missing: trail, rule, listener) | armed |
| `pr.revision` | tracked PRs with `pending_revision` | approve · normal | the review's time | Approve the revision (`…/revision/approve`); Dismiss (`…/revision/dismiss`); Open on GitHub | the reviewer's comment(s), the files in scope, the planned change | approved or dismissed |
| `pr.needs_human` | tracked PRs with `watch.status == needs_human` | fix · normal | `watch.since` | Open on GitHub (href); Resume (`…/resume`) once fixed; Pause | the reason (`scope_expanded`, `contradicts_intent`, …) in words, the comment it came from | the watch leaves `needs_human` |
| `job.failed` | jobs in `failed` or `dead` among the newest 50 | fix · urgent | `updated_at` | Retry (`/api/jobs/{id}/restart`); Acknowledge (snooze, section 3.8); Open job | `captain.command`, the error, the last five events, requester | a newer job of the same `captain.command` completed, or acknowledged |
| `access.finding` | open findings of the self-audit | fix · urgent (`unexplained`), normal (`missing`, `not_under_code`) | `first_seen` | Open the access PR (`/api/access/pr`); Audit now; Open access | role, kind, the statement or policy ARN, detail | the next audit no longer reports it |
| `update.available` | `update.available` and `latest` not dismissed | later · info | the release's date | Apply (`/api/updates/apply`, when the updater runs) else Open updates; Dismiss until next (snooze) | version, headline, the notes' first lines, how it would be applied | applied or dismissed |
| `setup.incomplete` | no read role; no GitHub connection; no alert queue; no model (info) | fix · urgent (account, GitHub), normal (queue), info (model) | the install's first start | Open settings (href to the section) | what is missing and the one command or click that fixes it | the thing is configured |

A product captain registers more with
`compass_api.extensions.register_inbox_source(name, fn)`, where
`fn(config, ctx) -> list[item]` returns items in this shape; the FinOps
fixes awaiting approval are the first candidate. A source that raises is
logged and skipped, never breaks the list.

### 3.4 The API

`GET /api/inbox` (scope `read`):

```json
{"generated_at": 1792670100000,
 "counts": {"total": 6, "urgent": 2,
            "by_tab": {"honeypots": 2, "prs": 1, "queue": 1, "settings": 2},
            "by_verb": {"approve": 2, "fix": 3, "merge": 0, "later": 1}},
 "items": [ … newest first … ]}
```

Query: `tab=`, `kind=`, `verb=`, `include_snoozed=true`. The list is
computed per request from the stores (open incidents, grants, the PR
registry, the newest 50 jobs, open findings, the cached update check, the
bootstrap's connection summaries) and cached in-process for 5 seconds per
workspace, so the sidebar's poll and a feed's poll cost one computation.

`POST /api/inbox/{id}/snooze` (scope `operate`) for the kinds of section
3.8; `DELETE` un-snoozes. `/api/inbox/{id}/decline` for `decoy.approval` is
the one new write beside snooze; every other action is a route that exists.

`GET /api/status` gains an `inbox` section: the counts and the three newest
items' titles. Not an API version bump.

### 3.5 The dashboard

- **Counts.** Beside each tab in the sidebar, the tab's count (urgent in the
  red chip, else amber). Beside the workspace name at the top, the total,
  which opens the whole feed. On phones, the total on the menu button and the
  same per-tab counts in the drawer. Counts come from `GET /api/inbox`
  alongside the sidebar's 15 s poll, and on `beamreach:queue-updated`,
  `beamreach:target-updated` and a new `beamreach:inbox-updated` that every
  inline action fires.
- **The feed.** One component, `InboxFeed({tab?})`: newest first, severity
  stripe, verb chip, title, summary, "since" as relative time, the primary
  action as a button and the rest in a small menu, a chevron that expands
  the detail. Expanding renders `detail` by kind with one small renderer per
  kind (risk breakdown as a list, statements as a code block, events as a
  log, comments as quotes); a kind the dashboard does not know renders the
  detail as key–value rows, so a product kind works before it has a renderer.
  An action with `confirm` shows the sentence and a second click; the call
  goes to the item's `path`, the result replaces the item's row with a
  one-line outcome for five seconds ("ran: verified in 41 s", "accepted:
  pull request #58 opened"), then the feed refreshes. A failure shows the
  API's detail on the row.
- **Where it shows.** Each tab with items gets a `needs_you` view (the
  Pull requests tab's existing one becomes this; Decoys, Queue and Settings
  gain one) that renders `InboxFeed({tab})`; the view's label carries the
  count as the PR tab's does today. The whole feed is `?tab=inbox` (not in
  the nav; reached from the total). Settings' view is a section at the top
  of the page when it has items. The update banner and the setup warning
  stay where they are and are the same items: dismissing the banner is the
  `update.available` snooze; the warning's text is the `setup.incomplete`
  item's summary.
- **Empty states.** A tab's view: "Nothing needs you here." The whole feed:
  "Nothing needs you. The daemon is running, N jobs today, next scan at …"
  (from `/api/status`).
- **Demo mode.** Items show "by a visitor" where they would show a user.
  The Trip cooldown is not an item.

### 3.6 The CLI and the MCP server

- `compass inbox [--tab <tab>] [--verb <verb>] [--json]`: the feed as a
  table (since, verb, title, the primary action's key). `compass inbox act
  <id> <action>`: performs the item's action by key (the CLI posts to the
  item's path with its body, after printing the `confirm` sentence and
  asking, unless `--yes`). `compass inbox snooze <id>`.
- MCP: `compass_inbox` (read; the same list) and `compass_inbox_act`
  (operate; `id`, `action`). Their descriptions tell an agent to read before
  acting and to show the `confirm` sentence to the person.
- `compass status` prints one line after the update line: `Needs you: 6 (2
  urgent): 2 to approve, 3 to fix, 1 later · compass inbox`.
- The `compass-status` plugin skill names the line; a new `compass-inbox`
  skill says how to triage: read, act on the urgent ones with the person's
  go, never act on `approve` items without showing the confirmation.

### 3.7 Ordering and the stripe

Newest `since` first, whatever the kind (decision taken). The stripe is
the severity: red for `urgent`, amber for `normal`, none for `info`. The
total chip is red when any item is urgent. Within a tab's view the same.

### 3.8 Snooze

Only these kinds; the condition is the kind's, not a duration:

| Kind | Snooze means | Ends when |
|---|---|---|
| `job.failed` | Acknowledge | a newer job of the same `captain.command` fails again |
| `update.available` | Dismiss until next | a newer release appears (today's rule) |
| `setup.incomplete` (model only) | "Running model-free on purpose" | a model key appears, or the config flag is cleared |

Stored as `INBOX#SNOOZE#<id>` rows with the ending condition's key; the
list filters them out and `include_snoozed=true` shows them greyed. Nothing
else can be hidden: an approval is run or declined, a grant accepted or
declined, a finding fixed.

### 3.9 Later: the digest

The same list, once a day at a configured hour, to the workspace's Slack
and email endpoints through today's notification routing, with each kind's
severity (`captains/prs/notify.py:18-31` is the pattern). Out of scope here;
the item shape is what it needs.

## 4. What changes where

- **Core.** `compass_core/inbox.py`: the item model, the kinds of section
  3.3 as one function each, `items(config, ctx)`, the cache, snooze rows,
  the `register_inbox_source` seam (in `compass_api/extensions.py` beside
  `status_sections`). `compass_core/keys.py`: `INBOX#SNOOZE#`.
- **API.** `compass_api/routes/inbox.py`: `GET /api/inbox`, snooze,
  decline. `routes/honeypot_incidents.py`: `…/decline`. `routes/status.py`:
  the `inbox` section. Scopes: reads `read`, writes `operate`
  (`compass_api/auth/scopes.py` defaults cover it).
- **Dashboard.** `components/InboxFeed.tsx`, `InboxCount.tsx`; `Sidebar.tsx`
  counts; `WorkspaceShell.tsx` the total on the menu button; the `needs_you`
  views (manifests of honeypot and queue gain the view; Settings' is a
  section); `PRsTab.tsx` drops its own filter for the feed; the banners
  read their items. `npx tsc --noEmit`.
- **CLI.** `cli/src/tools/inbox.ts` (`compass inbox`, `act`, `snooze`;
  the two MCP tools); `status.ts` the line; `cli/plugin/skills/compass-inbox/SKILL.md`,
  `compass-status` updated. `cli/tests`.
- **Docs.** `docs/inbox.md` (the kinds, what each asks, snooze, the CLI);
  `docs/architecture.md` one paragraph and the seams table
  (`Inbox sources | compass_core.inbox | register_inbox_source`);
  `docs/writing_a_captain.md` §: contributing items; the `compass-open` skill
  step that says what to check after install mentions `compass inbox`.
  `CHANGELOG.md` under the next release.
- **Tests.** `tests/compass_core/test_core_cli.py` pins `inbox`.

## 5. Test strategy

- **Kinds** (`tests/compass_core/test_inbox.py`, moto): one test per kind:
  the state that produces the item, the item's id, verb, severity, since,
  actions and detail, and the state change that removes it. The 50-job
  window; a dead job; a finding that the next audit drops; the update
  dismissed; setup with and without a model.
- **Determinism**: two calls within 5 s return the same `generated_at`;
  ids are stable across calls.
- **Seam**: a registered source's items appear; a source that raises is
  skipped and logged.
- **Routes** (`tests/api/test_inbox.py`): filters, scopes (a `read` token
  gets the list and is refused the actions), snooze and un-snooze, decline.
- **Dashboard**: `tsc`; e2e (`e2e/test_14_inbox.py`, shard A): the counts
  appear after a parked approval (`test_dec_7`'s setup), a failed job
  (`test_job_2`'s), a `needs_human` PR (`test_prs_2`'s) and a finding
  (`test_acc_5`'s); the whole feed lists all four newest first; expanding
  shows the risk breakdown; "Run" from the feed runs the action and the
  item leaves; the Pull requests `needs_you` view still passes `test_prs_2`;
  the phone drawer shows the total.
- **CLI**: `compass inbox` renders the list; `act` prints the confirmation
  and refuses without `--yes`.

## 6. Risks

- **Cost of the list.** `list_jobs` scans the jobs table
  (`compass_core/jobs.py:524`, roadmap 8.1); the PR registry and incidents
  are queries. With the 5 s cache the sidebar's poll costs one computation
  per 5 s per workspace; at a few hundred jobs that is tens of
  milliseconds. 8.1 removes the scan; the inbox does not wait for it.
- **Acting from a list.** A "Run" on a parked response from the feed is the
  same call as from the incident; the `confirm` sentence and the second
  click are the guard, and the expand shows the risk breakdown first. The
  scope on the token is enforced by the route, not the item.
- **Two places for one thing.** The banner and the feed show the same
  update; the PR tab's view and the feed show the same PR. They read the
  same items, so they cannot disagree; the risk is visual repetition, which
  the sidebar count makes acceptable.
- **A kind that lies.** A derivation that keeps an item after the state
  changed (a decline that leaves the pending entry) is the bug class here;
  every kind's test ends with the state change that removes the item.
- **Merge items are noise for some.** `grant.waiting` and `decoy.waiting`
  are `info` and say "merge on GitHub"; they are filterable by verb and
  never urgent.

## 7. PR plan

Each shippable alone, with a stop after each:

1. **`core: the inbox, derived from state`**. `compass_core/inbox.py` with
   every kind, the seam, the cache, snooze; `GET /api/inbox`, snooze and
   decline routes; the `/api/status` section; tests. *Stop: the JSON for a
   workspace with one of each kind, from the e2e stack.*
2. **`dashboard: counts on every tab and one feed`**. The components, the
   views, the banners on items, the mobile total. *Stop: screenshots light
   and dark, phone and desktop, with the six items of the mockup.*
3. **`cli: compass inbox and the MCP tools`**. *Stop: the table, an `act`,
   the status line, the skill.*

## 8. Decisions taken (2026-10-22, from the questions)

1. Counts on every tab, no new sidebar item; the whole feed opens from the
   total beside the workspace name; each tab has its own "Needs you" view.
2. Act in place, with expand on click showing enough to decide plus the
   links to the item's place.
3. One feed, newest first; the verb is a chip and a filter, not a grouping.
4. In: parked decoy responses, grants to accept, PRs the watcher gave up on
   and pending revisions, failed or dead jobs, audit findings, an available
   update, an incomplete setup, and items waiting on a merge on GitHub. Out:
   open incidents with nothing pending.
5. No dismiss; snooze only where the kind has a natural end.
6. The CLI and the MCP server now; a Slack digest later.

## Open decisions

**1. Where the whole feed opens from.** (a) the total beside the workspace
name at the top of the sidebar (specified); (b) `/workspace` lands on the
feed whenever anything is urgent, else on the default tab. *Recommendation:
(a)*, with (b) as a per-browser preference later: a map that opens on a list
surprises the person who came for the map.

**2. Declining a parked response.** (a) a new `…/decline` route that records
the decision and drops the pending entry (specified); (b) no decline: the
entry stays until the incident is closed. *Recommendation: (a).* Without it
an item a person has decided against stays in the feed until someone closes
the incident, which teaches people to ignore the feed.

**3. The jobs window.** The newest 50 (specified) versus every failed job
until acknowledged. *Keep 50*: an install that fails a scan nightly for a
month should not open with thirty items; the newest is the one to fix, and
"acknowledge" ends when it fails again.

**4. "No model" as a setup item.** (a) an `info` item with a
"model-free on purpose" snooze (specified); (b) not an item. *Recommendation:
(a)*: drift-fix HCL fails without a model, and a new install should learn
that from the inbox, not from a failed job.

**5. The stripe's levels.** Three (`urgent`, `normal`, `info`; specified)
versus two. *Three*: `info` is what lets the merge items and the update sit
in the feed without looking like problems.

**6. Polling.** Counts with the sidebar's 15 s poll and the window events;
the open feed every 10 s. *As specified*; a push channel is angle 4.6's
problem (the run view) and the inbox will use it when it exists.
