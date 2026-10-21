# Demo mode: a synthetic account, a real decoy, a tour, on demo.beamreach.ai

Status: accepted 2026-10-21; revised the same day to add the 0.7.0 release
(section 3.12), the map store on ECS (section 3.13) and the visitor's
definition of done (section 10). Roadmap item 4.9 (angle 4).
Line references: `main` of this repository at ed5590a (§3.12–3.13 at e7ed309); `beamreach-coworker` at cb76d19.

**The outcome this spec is for:** demo.beamreach.ai is a slick environment
strangers use on their own: it opens fast on a phone or a laptop, every tab
has something real in it, the map is big enough to be worth exploring, one
button trips a real decoy and the response lands while they watch, nothing
is broken or empty, and the last screen sends them to the install page.
Section 10 turns that sentence into a checklist; the work is done when the
checklist passes on demo.beamreach.ai, not when the PRs merge.

## 1. Goals and non-goals

**Goals.**

- A public demo at demo.beamreach.ai where a visitor, in ten minutes and
  without an AWS account, sees a 10,000-resource map, opens a real pull
  request, trips a real decoy, and watches the response land, then is sent to
  the install instructions.
- **Demo mode** is a feature of this repository: the synthetic world, the
  demo signalling, "Trip it", the tour and the reset ship in the open image,
  so the product build (which vendors this core) turns them on with
  configuration. Nothing of the demo is hand-wired in the environment.
- The map is synthetic (the benchmark world, 10k resources); the account and
  GitHub are real (`beamreach/demo`, the replay target `682684724085`;
  github.com). The two coexist on one map and one real rescan never removes
  the synthetic part.
- Writes are allowed. Everything a visitor does is attributed, paced where it
  touches the real account, and undone on Sunday.
- Nobody mistakes the demo for their account: a badge and a map watermark that
  stay in every screenshot and recording.
- The tour's steps double as the recording pipeline's beats
  (`demo/recording`), so the feature videos are made from the same world.

**Non-goals (for now).**

- Per-visitor isolation. One shared world, reset on Sunday (decision taken;
  section 3.7). The product's per-user workspaces stay off for the demo.
- A local demo profile for the open quickstart. Demo mode is designed so
  `docker compose --profile demo up` could turn it on against the e2e fakes,
  and the e2e suite runs it that way (section 7), but the quickstart does not
  advertise it in this release.
- The map at scale (4.2) and the paged query API (1.3). Demo mode is their
  first real user and is sized to what the current map draws until they ship
  (open decision 3).
- Changing the replay acceptance test (`demo/replay`). It stays the test of
  the real path; the demo reuses its reset step.
- Anything in the website: the install page exists
  (https://beamreach.ai/compass/install.html) and is the call to action.

## 2. Current state

**demo.beamreach.ai today** (`beamreach-coworker/deploy/environments/demo/main.tf`).
The product build (the enterprise engine with Celery and Redis, the open core
vendored as the `compass/` submodule **pinned at v0.4.1**), on ECS in
`eu-central-1` (control account `662863386798`), behind Cognito with Google
sign-in (`:289-299`), as `app_mode = "demo"`, `read_only = false`,
`default_tab = "finops"`, `disabled_features = "queue,compliance,security,maps,policies"`
(`:141-152`), a shared workspace `beamreach-demo` (`workspace_identity_mode =
"shared"`, `:142-144`), the target `beamreach/demo` (`:159`), and a
`production_url` that is the Marketplace CloudFormation quick-create
(`:147`). A `demo-local` variant runs the same on a Mac through a Cloudflare
tunnel (`deploy/environments/demo-local/`). Images are built and rolled by
`scripts/build_demo_images.sh`. The dashboard image is the open dashboard
plus `app/product/` (`images/Dockerfile.dashboard:8-11`), so a dashboard
change here reaches the product by a submodule bump. There is no scheduler of
any kind in `deploy/` (no `schedule_expression`, no cron).

**The real decoys.** The replay environment in `beamreach-ai/beamreach-demo-infra`
(`modules/replay`, a Next.js ECS service `web` in cluster `public-demo-replay`,
`us-east-1`) carries two planted, armed decoys (`aws_credential` in the task's
environment, `db_credential` with a listener), two enabled responses
(`ecs_restart_task` scoped to the service, `iam_deactivate_key`, both
autopilot, `risk_max=2`), a Slack channel and an email endpoint, and the alert
queue `beamreach-honeypot-alerts-beamreach-demo-cloud` with its consumer
service (`docs/honeypot_replay_spec.md:244`). `demo/replay/run.sh friday`
harvests the credentials from inside the task, uses both, and waits for the
task to be replaced; measured containment is about +51 s after the first use
(`honeypot_replay_spec.md:294`). `run.sh reset` closes incidents, rolls back
and re-arms (`check.py:149`). **The hazard recorded there**: the infra repo's
workflow applies `main` after an environment approval that did not block, and
one merge destroyed the replay environment; the rule since is that a
targeted apply is safe only if its PR merges before the next merge to `main`.
That repository must never receive a visitor's pull request.

**What the open edition already has.**

- *App mode and read-only.* `APP_MODE` ∈ {production, demo} and `READ_ONLY`
  (`compass_api/routes/_app_config.py:57-60`); `write_guard` refuses writes
  only when read-only (`:98-116`). The dashboard unlocks the tabs in demo
  mode without a connected account (`DashboardClient.tsx:125`), shows a
  banner whose text assumes read-only (`:560`, "Demo mode is read-only…"),
  and in the sidebar shows "Self-host on AWS" to `production_url`
  (`Sidebar.tsx:224`, `:380-387`). The shell was built for a stand opened from
  phones (`WorkspaceShell.tsx:10`). `honeypotFormat.ts:9` and `HoneypotsTab.tsx:1915`
  disable controls on a 403. There is no badge, no watermark, nothing that
  marks a screenshot.
- *The synthetic world.* `tests/benchmark/world.py`: `WorldSpec(resources,
  regions, managed_share, drifted_share, seed, org_id, account)` → `World`
  with nodes, edges, hubs, typical and managed ids, built with the collectors'
  own id helpers; `churn()` is the next day's scan. At 10k: 16,104 nodes and
  26,166 edges, four regions, 60% managed, 3% drifted, power-law hubs
  (`docs/benchmark.md`, "The world"). It is written to a store through
  `GraphStoreAdapter.scan(world)` (`tests/benchmark/adapters.py:30`), which is
  `begin_scan` + `put_nodes` + `put_edges` + commit. It lives under `tests/`.
- *Scopes.* A scan closes only the rows within its own `(source, scope)` pairs
  (`infra_map/graph_store.py:171-173`, `:202`; `docs/specs/map-store.md` §3.4
  step 3). A world written under a source the mapper never scans is never
  closed by the mapper.
- *The map's limit.* The dashboard requests 4,000 items and draws them all
  (`MapsTab.tsx:1005`); the summary-first load and draw-on-demand (map-store
  PR 6) have not shipped (`CHANGELOG.md` 0.6.0: "the API for that comes in a
  later release").
- *Firing a decoy.* `honeypot fire` makes one STS call with the decoy's key
  and waits for the alert (`captains/honeypot/commands.py:224-241`); the key
  comes from the token's stored `material` (`:236-238`). The API has
  `test-fire` (`compass_api/routes/honeypots.py:843`, an `is_test` alert that
  opens no incident), `material` (`:566`) and `arm` (`:910`), and no route
  for a real fire. The e2e suite trips a decoy by posting the alert message
  straight to the queue (`e2e/test_06_decoys.py:87`).
- *Fakes.* moto and a fake GitHub with reviews, merges and a control API
  (`e2e/fakes/app.py:588-692`), wired by `e2e/compose.e2e.yml`; `connect`
  runs against moto (`e2e/conftest.py:302`).
- *Tabs and features.* The open tabs are maps, iac, prs, honeypots, queue
  (`dashboard/app/lib/captains.tsx:50-54`); feature flags in
  `compass_core/features.py:6-31`.

**What is missing**, in one list: a shippable world generator and a command
to seed it under its own source; demo signalling that survives a screenshot;
a real-fire route with pacing and re-arming; a tour; a reset; the Terraform to
turn it on and schedule the reset; and a core recent enough in the product
(the product pins v0.4.1; demo mode needs the SQLite store with scopes, 0.6,
and the access model, 0.7).

## 3. The design

```
 visitor ── Google sign-in ──▶ demo.beamreach.ai (product build, APP_MODE=demo, READ_ONLY=0)
                                 │
                                 ├─ map ──── synthetic world, source demo_world (seeded once)
                                 │           + the real account, source aws_scan (rescanned every 6 h)
                                 ├─ PRs ──── beamreach-ai/compass-demo-sandbox (real GitHub; no apply workflow)
                                 ├─ decoys ─ pre-planted, armed, in beamreach-demo-infra (replay service)
                                 │           "Trip it": real STS call ─▶ CloudTrail ─▶ queue ─▶ incident ─▶ response
                                 │           cooldown 15 min per decoy, shared; auto re-arm after the response
                                 └─ tour ─── 7 steps, completion read from the state above, per signed-in visitor
 Sunday 03:00 UTC: compass demo reset (EventBridge Scheduler → ECS RunTask)
```

### 3.1 Vocabulary

- **Demo mode** — `APP_MODE=demo` with `READ_ONLY` off and a `demo` block in
  the workspace config (section 5). The dashboard, the API and the daemon read
  the same flag.
- **Golden world** — what the demo looks like after a reset: the synthetic
  world, the real account's scan, the pre-armed decoys, one seeded pull
  request with a reviewer's comment, no incidents, no visitor PRs.
- **Synthetic world** — the benchmark generator's output, written to the map
  under `source = "demo_world"`, `scope = "seed:<n>"`. Its nodes carry the
  real target's account id and the attribute `synthetic: true`.
- **Sandbox repo** — the real GitHub repository visitors' pull requests go
  to, `beamreach-ai/compass-demo-sandbox` (open decision 1). It has no apply
  workflow, branch protection with no required reviews, and the GitHub App
  `beamreach-compass-demo` installed.
- **Pre-armed decoys** — the replay environment's two decoys, planted from
  `beamreach-demo-infra`, armed once by us. The only decoys "Trip it" fires.
- **Trip** — a real fire of a pre-armed decoy by a visitor, through the new
  route, under a cooldown.
- **Reset** — `compass demo reset`: back to the golden world. Idempotent,
  scheduled for Sunday, runnable by hand.
- **Tour** — seven steps in a panel, each completed by a fact about the
  world, kept per signed-in visitor.

### 3.2 The world

The generator moves from `tests/benchmark/world.py` to `infra_map/synthetic.py`
(the benchmark imports it from there; nothing else changes for the benchmark).
It gains `source` and `scope` on every node and edge it emits, so a world is a
scan like any other.

`compass demo seed [resources=10000] [seed=7]`:

1. Refuses outside demo mode.
2. `begin_scan(org_id, scopes=[("demo_world", "seed:7")])`, puts the world's
   nodes and edges with the target's account id (`aws.account_id`), commits.
   A second run with the same seed is a no-op scan (every row unchanged); a
   different `resources` or `seed` closes the old world and writes the new
   one under the new scope.
3. The mapper's own scans keep their scopes (`aws_scan`, `github`,
   `tf_state`), so the real account's ~100 resources sit beside the synthetic
   16,000 and a rescan touches only its own. `COMPASS_MAP_INTERVAL` is set to
   six hours in the demo so the real part stays current without the daemon
   scanning all day.
4. Ownership nodes in the synthetic world name synthetic repositories
   (`github_repo_node_id("acme/...")`); the IaC coverage tab shows 60%
   managed, the suggestions ranker sees the synthetic services. A synthetic
   node's detail panel says "Synthetic resource (demo world)" under its name,
   and every write that starts from one (plant, import) says so in the PR
   body. The synthetic world is a map to explore; the real resources are what
   the tour's writes touch (section 3.6).

Size is a config knob, `demo.world.resources`, because the map draws 4,000
items today (open decision 3).

### 3.3 Signalling

In demo mode, in the open dashboard (so the product inherits it):

- **Badge.** A `DEMO` chip beside the account in the sidebar, in the page
  title (`Compass · demo`), and in the mobile top bar. Not dismissable.
- **Watermark.** `DEMO` in the lower-right corner of the map canvas and of
  every exported map page, drawn above Graphin's canvas so it is in the
  pixels of a screenshot. Not dismissable.
- **Banner.** One line at the top, dismissable for the session: "Demo
  account. Plant, open pull requests, trip a decoy and watch the response: it
  all runs against a shared demo account that resets on Sunday." The read-only
  text at `DashboardClient.tsx:560` stays, shown only when `read_only` is also
  on.
- **Sidebar.** "Self-host on AWS" becomes "Run it yourself" →
  https://beamreach.ai/compass/install.html (`production_url` keeps its
  meaning as the link's target; the demo sets it to the install page). The
  GitHub repository link sits under it.
- **Attribution.** Every write a visitor makes shows "by a visitor" in lists
  and "by you" on their own; the identity behind it is the Cognito user id
  in the audit row and in `SourceIdentity`, never an email on screen (open
  decision 2).

### 3.4 Writes

Visitors can do everything the dashboard offers. What each write touches:

| Write | Where it lands | What the visitor sees |
|---|---|---|
| Plant a decoy (any class, any placement, synthetic or real) | A pull request on the sandbox repo, label `demo`, body: "Opened from demo.beamreach.ai by a visitor. This repository is never applied; in your install you would review and merge it. Closed on Sunday." | The PR on github.com, the token in `pr_open`, and a note on the token: "Arming needs an apply; the decoys you can trip are the two pre-armed ones." |
| Import a resource | Same repo, same label; `terraform plan` runs against the real account with read credentials, so an import of a real unmanaged resource verifies and one of a synthetic resource reports `verified: false` with the reason "synthetic resource" | The PR, and the IaC tab's verified flag |
| Fix drift | Same | Same |
| Answer a reviewer, revise a PR | The seeded PR on the sandbox repo (a reviewer's comment by our bot account, recreated by the reset) | The PR watcher's "Needs you" and the reply or revision on github.com |
| Enable or remove a response action | A grant: the access PR on the sandbox repo (3.5 of the access model), never applied | The grant in `pr_open`; the pre-armed responses stay `armed` |
| Settings | Allowed, except what the demo pins (section 5): the GitHub connection, the target, the demo block, notification endpoints' secrets | Pinned fields read-only with "fixed in the demo" |
| Trip a decoy | The real account (section 3.5) | The incident |

The PR watcher syncs the sandbox repo as it syncs any repo; the daemon's
`prs-tick` runs every 60 s in the demo (300 s elsewhere) so a reply shows up
while the visitor is looking.

### 3.5 Trip it

`POST /api/honeypots/tokens/{id}/trip` (demo mode only; `operate` scope;
404 outside demo mode):

1. The token must be one of the pre-armed decoys (`demo.trippable` lists
   their ids) and `armed`. Anything else → 409 with the reason.
2. **Cooldown.** One trip per decoy per 15 minutes across all visitors, kept
   as a row `DEMO#COOLDOWN#<token>` with `until` and the incident id that
   trip opened. During the cooldown the route answers 200 with
   `{"state": "cooldown", "incident_id": …, "until": …}` and the dashboard
   takes the visitor to that incident: "Tripped 6 minutes ago by a visitor;
   the incident is open below." The button shows the countdown.
3. **Fire.** The real fire from `commands.fire`, server-side, with the stored
   material, as a job (`honeypot trip`), attributed to the visitor
   (`SourceIdentity`, the job's requester). The job emits events the tour
   polls: `fired_at`, `alert_received_at`, `incident_id`.
4. **What follows is the real path**: CloudTrail (one to five minutes) →
   EventBridge → the queue → the consumer → the incident → the investigation
   → the gate (autopilot, risk ≤ 2) → `ecs_restart_task` and
   `iam_deactivate_key` on the replay service, exactly as the replay
   measures it. The incident page shows the timeline with the clock running.
5. **Re-arm.** When the incident's `action_verified_at` is set, the demo
   service (`demo-rearm`, a core `ServiceSpec` active only in demo mode,
   every 30 s) rolls back `iam_deactivate_key` through the existing rollback
   (`captains/honeypot/actions/iam_deactivate_key.py:89-101`) and re-runs
   `arm` for the token; the `db_credential` decoy needs no rollback (the
   restarted task has the same environment). The incident stays open for
   other visitors to read until the reset. The cooldown row keeps the
   incident id so late arrivals see it.
6. **Guard rails.** The replay service's desired count is 1; a restart is a
   single task replacement; the responder role's policy allows nothing else
   (the access model's act role). The route is rate-limited per visitor (3
   trips an hour) on top of the shared cooldown, and the WAF's 1,000 requests
   per 5 minutes stays.

What a visitor cannot do: trip their own planted decoy (never applied), fire
a test (the `test-fire` route is hidden in demo mode; it would confuse the
story), or run a response by hand (the pre-armed rows are autopilot; the
"Run" button on a parked approval works as it does today for a grant of
their own, which never arms).

### 3.6 The tour

A panel in the open dashboard (`TourPanel`, bottom-right, collapsible to a
pill "Tour · 3 of 7"), opened on a visitor's first sign-in, resumable from
the pill. Steps are facts about the world, read by the client from
`GET /api/demo/tour` every 10 s while the panel is open; a client-only step
posts its completion (`POST /api/demo/tour/{step}`). State is per Cognito
user, a row `DEMO#TOUR#<user>`, so a visitor can switch from the phone they
scanned the stand's QR code with to a laptop.

| # | Step | Opens | Done when |
|---|---|---|---|
| 1 | **See the account.** 10,000 resources across four regions; search `payments`, open a role, follow what it can reach. | Maps, a search pre-filled | The visitor opened any node's detail (client event) |
| 2 | **What Terraform doesn't manage.** 40% of it; the drift on the queue. | IaC | The IaC tab was viewed (client event) |
| 3 | **Open an import pull request.** Import `acme-legacy-exports` (real, unmanaged, in the replay account). | IaC, the resource selected | A PR by this visitor exists on the sandbox repo (API) |
| 4 | **Meet the decoys.** Two armed decoys next to the replay service's real secret; where they sit and why. | Decoys → honeytokens | The token detail was viewed (client event) |
| 5 | **Trip one.** "Trip it" on `svc_backup`'s key; the clock starts. | The token, the button highlighted | An incident exists for that token opened after this visitor pressed Trip, or a cooldown was in force and the visitor was shown the open incident (API) |
| 6 | **Watch the response.** The investigation, the blast radius, the gate, the restart, the key going inactive, in under a minute after the alert. | The incident | `action_verified_at` set on that incident (API) |
| 7 | **Run it yourself.** The install page; the repository; "connect uses your profile once". | A card with both links | A link was clicked (client event) |

Optional side step after 3, shown when the seeded PR has a comment: **Answer
a reviewer** (Pull requests → Needs you). Done when a reply by this visitor
exists on that PR.

Each step has a one-line "why this matters" and a "Skip". The last step's
card is the only call to action in the demo; the sidebar's "Run it yourself"
points at the same page.

**The beats.** `demo/recording/beats/tour.js` in the coworker repository
drives the seven steps with the synthetic cursor against demo.beamreach.ai;
the recording pipeline's `dry` run is the nightly check that the demo still
walks (section 7). The step texts in the panel are the captions' source.

### 3.7 Reset

`compass demo reset`, idempotent, in this order, each step logged:

1. Close every open incident with resolution `demo reset`; roll back every
   `iam_deactivate_key` audit not yet rolled back; re-arm the pre-armed
   decoys; wait for the replay service's steady state (what `run.sh reset`
   does today, moved into core so the environment runs it without the
   replay scripts).
2. Close every pull request on the sandbox repo (through the GitHub App),
   delete their branches, retire the tokens and grants they belonged to
   (`decommissioned`, no decommission PR).
3. Recreate the seeded pull request with the reviewer's comment (one fixed
   file change, one comment from the bot account).
4. Prune jobs older than seven days, clear every `DEMO#COOLDOWN#` and
   `DEMO#TOUR#` row, and the dismissed-banner state is per session anyway.
5. Re-seed the synthetic world (a no-op scan when unchanged); start a real
   rescan.
6. Write `DEMO#RESET#<ts>` with counts, and post one line to the demo's
   Slack channel.

Scheduled by EventBridge Scheduler, `cron(0 3 ? * SUN *)` UTC, as an ECS
`RunTask` of the worker image with the command `compass demo reset`, in the
coworker Terraform (there is no scheduler there today). `compass demo
status` prints the last reset, the next one, cooldowns in force, visitor PRs
open, tour completions this week.

### 3.8 Sign-in and attribution

Google sign-in through Cognito stays as it is. The visitor's Cognito user id
is the requester of every job, the approver of nothing (the pre-armed
responses are autopilot; a grant of their own never arms), and the
`SourceIdentity` of the act sessions their trip causes, so the replay
account's CloudTrail attributes each `StopTask` to a visitor id. On screen,
other visitors' writes say "a visitor" (open decision 2).

### 3.9 Tabs and landing

`DISABLED_FEATURES="queue"`; `DEFAULT_TAB=maps`. Every product tab on: Maps
(landing), Decoys, IaC, Pull requests, FinOps, Audit, Settings. The Queue tab
is operator plumbing and stays off.

### 3.10 Analytics

If usage analytics (angle 10) ships first, the demo's install id is flagged
`demo: true` and excluded from the public stats.

### 3.11 Dependencies and what they decide

- **The product's core pin.** v0.4.1 today; demo mode needs the SQLite store
  with scopes (0.6.0) and the access model (0.7.0), so the first coworker
  change is the submodule bump to the release that carries demo mode, and
  its migrations (`0.6.0-map-sqlite`, `0.7.0-access-model`) run on the demo's
  workspace. The replay environment's decoys and responder role become
  grants under the access model; `connect` runs once against the replay
  account with the `public-demo` profile.
- **The map at scale.** 4.2 and 1.3 are not shipped; the dashboard draws
  4,000 items. Open decision 3 sets the world size until they land.
- **A real-fire route.** New in this spec; nothing else needs it.
- **A released core.** Nothing has been released since v0.4.1: `main`
  carries 0.5.0 to 0.7.0, each section marked unreleased in `CHANGELOG.md`,
  and `pyproject.toml` still says 0.5.2. Section 3.12.
- **A map store the product's services can share.** Section 3.13.

### 3.12 The release: 0.5.0 to 0.7.0 released together as v0.7.0

Before any demo-mode code, the four unreleased versions ship as one release,
so the demo (and every install on 0.4.1) moves to a tagged core.

- `CHANGELOG.md`: the sections `0.5.0`, `0.5.1`, `0.5.2`, `0.6.0` and `0.7.0`
  lose "unreleased" and take the release date (`## 0.7.0 — 2026-10-2x`, and
  the same date on the four below it). The 0.7.0 section's first paragraph,
  which the dashboard shows as the summary, gains one sentence: "This release
  also carries 0.5.0 to 0.6.0, which were not released on their own." The
  "After the update" bullets of 0.6.0 (fetch the new compose file for the
  `map-data` volume) and 0.7.0 (run `compass connect` once) both apply to an
  install coming from 0.4.1, and the update card already lists the notes of
  every version in between.
- `pyproject.toml`: `0.7.0`. The release workflow refuses a tag that does
  not match it (`.github/workflows/release.yml:51-54`) and one without its
  changelog section (`:55-56`).
- One tag, `v0.7.0`, on `main`. The workflow runs the whole e2e suite on the
  tagged commit, publishes the three images, the GitHub Release, the entry
  in `releases.json`, and bumps the `COMPASS_VERSION` defaults on `main`
  (still `0.4.0` in `docker-compose.yml` and `.env.example`). The
  intermediate versions are not tagged and get no images: there was never a
  commit whose `pyproject.toml` said 0.5.0, 0.5.1 or 0.6.0.
- The release notes feed on beamreach.ai needs a page for each version the
  update card will list (0.5.0, 0.5.1, 0.5.2, 0.7.0; 0.6.0 has one). The card
  does not hold the update back when a page is missing; it shows less.
- One core fix goes into the release first (PR 0 in section 8): the
  `0.6.0-map-sqlite` migration (`infra_map/migrate.py:29-59`) copies into a
  SQLite file whatever store the config names and then points the workspace
  at that file. A deployment that sets `map.store: postgres` (the product,
  section 3.13) must get its map copied into Postgres and keep its setting.
  The step copies into the configured store, or skips when that store
  already has a scan.

Demo mode itself (PRs 1–5) ships in the release after, 0.8.0, and the demo
bumps twice: to 0.7.0 as soon as it is out (the large, risky bump, done
without any demo-mode code in it), then to 0.8.0.

### 3.13 The map store on ECS

The product runs the core as a dozen ECS services
(`deploy/modules/on-premise/main.tf:1254-2806`): the mapper captain writes
the map, the engine (the API), the IaC, honeypot and triage workers read it,
each its own Fargate task. The open default, a SQLite file on a volume
shared by two containers on one host, cannot serve that: Fargate tasks share
only EFS, and SQLite's WAL is unsafe on NFS (`docs/specs/map-store.md` §13).
DynamoDB, the product's map today, is the store the benchmark measured at 20
to 33 seconds per whole-graph read and two minutes per scan at 10k
(`docs/specs/map-store.md` §10.5): a map nobody would click twice.

The demo's map is therefore **Postgres**, which the core already supports
(`infra_map/graph_store.py:380-386`, `pip install compass[postgres]`,
`map.store: postgres` with `map.dsn` or `COMPASS_MAP_DSN`) and which met
every 10k budget but one (4.8 s per scan, 0.2 s first load, milliseconds per
read). In the coworker Terraform: one RDS PostgreSQL 16 instance
(`db.t4g.small`, 20 GB gp3, single-AZ, in the demo VPC's private subnets,
reachable from the services' security group only), the DSN in Secrets
Manager, `COMPASS_MAP_STORE=postgres` and `COMPASS_MAP_DSN` on every service
that touches the map, the `postgres` extra in the enterprise image.

Two product changes go with it, in the coworker repository:

- The engine and the triage captain read the map through the old DynamoDB
  class directly (`src/beamreach/captains/engine/context_builder.py:177`,
  `src/beamreach/captains/triage/captain.py:299`). They move to
  `infra_map.graph_store.open_store`, or they would read a map the mapper no
  longer writes.
- The demo's existing DynamoDB map is the small real account; the migration
  (fixed in PR 0) copies it into Postgres once, and the first rescan
  rebuilds it anyway.

The marketplace and production environments are out of scope here; they
inherit the decision when they bump, and this spec's coworker step is where
the pattern is set.

## 4. What changes where

**This repository.**

- `infra_map/synthetic.py` (from `tests/benchmark/world.py`; the benchmark
  imports it). `compass_core/demo.py`: `seed`, `reset`, `status`, the
  `demo-rearm` service, the trip cooldown. `COMMANDS` gains `demo`
  (`seed | reset | status`); `tests/compass_core/test_core_cli.py` pins it.
- `compass_api/routes/demo.py`: `GET /api/demo` (mode, world, trippable
  tokens, cooldowns, next reset), `GET/POST /api/demo/tour`.
  `compass_api/routes/honeypots.py`: `POST …/trip`; `test-fire` 404 in demo
  mode. `captains/honeypot/commands.py`: `trip` as a job wrapping `fire`.
- Dashboard: `DemoBadge` (sidebar, title, top bar), `MapWatermark` in
  `MapsTab` and the export page, the banner text split by `read_only`,
  `TourPanel`, the "Trip it" button and cooldown on the token card, "by a
  visitor" in lists, pinned settings read-only. `npx tsc --noEmit`.
- Config: a `demo` block (section 5); `app.mode` already in the bootstrap.
- Docs: `docs/demo.md` (what demo mode is, the commands, the world, the
  tour, the reset); `docs/architecture.md` one paragraph; `docs/e2e.md` the
  new file; `CHANGELOG.md` under the next release; the `compass-open` skill
  says nothing (the quickstart does not advertise demo mode).

**`beamreach-coworker`.**

- Submodule bump to the release with demo mode; `scripts/migrate_*` as the
  release notes say.
- `deploy/environments/demo/main.tf`: `disabled_features = "queue"`,
  `default_tab = "maps"`, `production_url = "https://beamreach.ai/compass/install.html"`,
  `demo_world_resources`, `map_interval = 21600`, `prs_tick = 60`; the
  `on-premise` module gains the EventBridge Scheduler rule and the `RunTask`
  target for the reset, and `DEMO_*` env for the worker.
- The sandbox repository created, the GitHub App installed on it, the bot
  account for the seeded review; the workspace's `honeypot_repo` and
  `iac_repo` set to it; `beamreach-demo-infra` stays the home of the
  replay environment and the pre-armed decoys.
- `demo/recording/beats/tour.js`; the recording README's nightly dry run.
- `docs/honeypot_demo_scenario.md`: a line that the public demo's "Trip it"
  is the same F3 path, paced.

**Website.** Nothing; the install page is the target.

## 5. Configuration

```json
"demo": {
  "enabled": true,
  "world": {"resources": 10000, "seed": 7, "source": "demo_world"},
  "trippable": ["<token id of svc_backup's key>", "<token id of the db credential>"],
  "cooldown_seconds": 900,
  "trips_per_visitor_per_hour": 3,
  "sandbox_repo": "beamreach-ai/compass-demo-sandbox",
  "seeded_pr": {"path": "services/payments/variables.tf", "reviewer": "beamreach-demo-reviewer"},
  "reset": {"schedule": "cron(0 3 ? * SUN *)", "prune_jobs_days": 7},
  "pinned_settings": ["github", "aws", "demo", "notifications.endpoints"],
  "links": {"install": "https://beamreach.ai/compass/install.html",
            "repository": "https://github.com/beamreach-ai/compass"}
}
```

`APP_MODE=demo` turns the mode on; the block says what the demo is. Outside
demo mode the block is ignored and the routes answer 404.

## 6. Test strategy

- **The world** (`tests/infra_map/test_synthetic.py`): seeding writes under
  `demo_world`; a mapper scan afterwards closes none of it; a second seed is a
  no-op (no new versions); a different size closes the old scope; the benchmark
  still imports and runs at 10k.
- **Trip** (`tests/api/test_demo_trip.py`, moto): a non-trippable token → 409;
  the cooldown row; the second caller within 15 minutes gets the incident id;
  the per-visitor limit; `test-fire` 404 in demo mode; re-arm after
  `action_verified_at` rolls back the key and re-arms (moto IAM, the fake
  arm).
- **Tour** (`tests/api/test_demo_tour.py`): each step's check against a seeded
  state; per-user rows; client-only steps need the post.
- **Reset** (`tests/compass_core/test_demo_reset.py`, moto + the fake GitHub
  from `e2e/fakes` run in-process): visitor PRs closed and branches deleted,
  the seeded PR recreated once, incidents closed, keys rolled back, tokens
  re-armed, cooldowns and tours cleared, jobs pruned, the world re-seeded as
  a no-op; a second run changes nothing and reports.
- **e2e** (`e2e/test_13_demo.py`, shard A): the e2e stack with
  `APP_MODE=demo` and a 2k world seeded through the CLI: the badge and the
  watermark are in a screenshot of the map (pixel check on the corner); tour
  steps 1–3 complete through the browser; the trip route is exercised with
  the fake alert path (`test_06:87`) standing in for CloudTrail, and step 6
  completes when the fake response verifies; the reset through the control
  API closes the visitor's PR. The existing read-only test (`test_aut_5`)
  keeps `READ_ONLY=1` as its own thing.
- **The live demo** (coworker, nightly): `npm run dry -- tour` against
  demo.beamreach.ai; fails on a selector or a step that does not complete.
  `demo/replay/run.sh friday` stays the acceptance test of the real path and
  runs by hand.

## 7. Risks

- **Strangers and a real account.** Bounded by: the trippable list (two
  decoys), the act role's grants (restart one service, deactivate the decoy
  users' keys), the cooldown and per-visitor limit, sign-in, the WAF, and a
  sandbox repo with no apply. The replay account holds nothing but the
  replay environment.
- **CloudTrail latency.** One to five minutes between "Trip it" and the
  incident. The tour says so and shows the clock; the previous visitor's
  incident is one click away while waiting. The replay's measured +51 s is
  from first use to containment, after CloudTrail delivered.
- **The 4,000-item map.** A 10k world is 16k nodes; today's dashboard cuts
  it at 4,000 without saying so. Decision 3.
- **The product pin.** Two major core releases behind; the bump carries two
  migrations and the access model's breaking change (`connect` on the demo
  workspace). Done once, by hand, before anything else.
- **Shared state at a conference.** Twenty people pressing "Trip it" see one
  incident and a countdown; the tour's step 5 is written for that. Weekly
  reset means a Friday crowd's PRs are still there on Saturday; the sandbox
  repo may hold a few hundred closed PRs a year.
- **Re-arm races.** Two trips cannot overlap (the cooldown row is a
  conditional write); the re-arm service waits for `action_verified_at` and
  for the key rollback before arming; a failed re-arm leaves the token
  `pr_open`-style "needs attention" and posts to Slack.
- **Synthetic resources in real flows.** A plant or import on a synthetic
  node opens a PR that could never apply; the PR body says so and the
  sandbox repo cannot apply anyway. The tour steers the writes to real
  resources.
- **Privacy.** Visitors' Google identities are in Cognito and in the audit
  rows; nothing shows another visitor's identity on screen; CloudTrail in our
  account holds user ids, not emails.

## 8. PR plan

This repository, each shippable alone, with a stop after each:

0. **`map: the 0.6.0 migration copies into the configured store`**, then
   **`release: 0.7.0`** (section 3.12): the fix, then the changelog dates,
   `pyproject.toml` and the tag. *Stop before the tag: the changelog diff,
   the green CI, and your go. Stop after: the workflow's run, the images,
   the Release.*
1. **`map: the synthetic world is a module; compass demo seed`**.
   `infra_map/synthetic.py`, the `demo` block and command, the scopes, the
   benchmark on the moved module. *Stop: a 10k world beside a real scan in
   one SQLite file, and the rescan leaving it alone.*
2. **`dashboard: demo mode is marked`**. Badge, watermark, banner by
   `read_only`, "Run it yourself", "by a visitor", pinned settings. *Stop:
   screenshots light and dark, phone and desktop.*
3. **`honeypot: trip a pre-armed decoy, paced, and re-arm after the response`**.
   The route, the job, the cooldown, the re-arm service, `test-fire` hidden.
   *Stop: a trip on the e2e stack through the fake alert path.*
4. **`demo: the tour`**. The API, the panel, the seven steps, the side step.
   *Stop: the tour walked on the e2e stack.*
5. **`demo: reset and status`**. *Stop: a reset on the e2e stack, then by
   hand against demo.beamreach.ai once 6 is done.*

Then `beamreach-coworker`:

6. **The bump to 0.7.0**, as soon as it is released, in parallel with PRs
   1–5: the submodule, the RDS instance and `COMPASS_MAP_*` (section 3.13),
   the engine and triage readers on `open_store`, the migrations, `connect`
   on the replay account, the existing responder role and pre-armed decoys
   under grants. *Stop: the demo as it is today, on 0.7.0, working: the
   replay's `friday` phase green.*
7. **The bump to 0.8.0 and demo mode on**: the submodule, the Terraform
   (variables, scheduler, run-task), the sandbox repo, the bot account, the
   seed. *Stop: section 10's checklist, walked by you on a phone and a
   laptop.*
8. **The beats and the nightly dry run.** *Stop: a dry run's output.*

## 9. Decisions taken (2026-10-21, from the questions)

1. The demo is demo.beamreach.ai; demo mode is the feature that runs it.
2. Signalling: badge, watermark and a dismissable banner; writes allowed.
3. The world: the synthetic benchmark world at 10k (size a knob; decision 3
   below).
4. A tour panel whose steps complete from real state.
5. The product build runs the demo, demo mode on.
6. Hybrid target: synthetic map, real account for decoys and plans.
7. One shared world, reset on Sunday.
8. Google sign-in stays.
9. Landing on Maps; every product tab on; Queue off.
10. Visitor writes open real pull requests that are never applied; the
    pre-armed decoys are what trips.
11. "Trip it" has a shared cooldown and re-arms after the response.
12. The call to action is the website's install page, not the Marketplace.
13. 0.5.0 to 0.7.0 are released together as v0.7.0, before demo mode;
    demo mode ships in 0.8.0 (section 3.12).
14. The scope stays as specified: the outcome is a demo visitors use on
    their own, and section 10 is its definition of done.

## 10. Definition of done: the visitor's walk

Walked on demo.beamreach.ai, signed in with a Google account that has never
used the demo, once on a phone (Safari, iOS) and once on a laptop (Chrome),
on the production build after step 7. Every line passes, or the work is not
done.

**Arriving**

- [ ] Sign-in to the map in under 5 s on the laptop, under 8 s on the phone
      on 4G; the map is drawn, not a spinner.
- [ ] The badge and the watermark are visible on the first screen; the
      banner says what the demo is in one line and can be dismissed.
- [ ] The tour opens by itself at step 1; closing it leaves the pill.

**Every tab**

- [ ] Maps, Decoys, IaC, Pull requests, FinOps, Audit and Settings each open
      in under 2 s, with no error toast, no "not configured", no empty state
      that asks the visitor to set something up.
- [ ] Maps: search finds `payments`; a node's detail opens; a synthetic node
      says so.
- [ ] IaC: coverage and the unmanaged list are populated; the drifted queue
      is there.
- [ ] Decoys: two armed decoys with "Trip it"; suggestions on the map's
      resources.
- [ ] Pull requests: the seeded PR with the reviewer's comment under Needs
      you.
- [ ] Settings: the pinned fields say "fixed in the demo"; nothing the
      visitor can save breaks the demo for the next one.

**The tour, start to end**

- [ ] Step 3 opens a real PR on the sandbox repo, visible on github.com,
      labelled `demo`.
- [ ] Step 5: "Trip it" to the incident on screen in under 6 minutes; the
      clock is visible while waiting; if in cooldown, the visitor lands on
      the open incident instead.
- [ ] Step 6: the incident shows the investigation, the blast radius, the
      restart and the key deactivated, under a minute after the alert.
- [ ] Step 7 opens https://beamreach.ai/compass/install.html.
- [ ] Every step's check completes without a reload; "Skip" works on each.

**On the phone**

- [ ] The drawer menu opens and closes; the tour pill does not cover a
      control; the map pans and zooms with a finger; the incident timeline
      reads without horizontal scrolling.

**After**

- [ ] A second visitor signing in five minutes later sees the first one's PR
      as "by a visitor", and their own tour at step 1.
- [ ] A manual `compass demo reset` brings the demo back to this list's first
      line, and the scheduled one is listed for Sunday 03:00 UTC.
- [ ] The nightly `npm run dry -- tour` is green.

## Open decisions

**1. Where visitor pull requests go.** (a) a sandbox repository,
`beamreach-ai/compass-demo-sandbox`, with no apply workflow, the pre-armed
decoys staying in `beamreach-demo-infra`; (b) `beamreach-demo-infra` itself,
with its apply workflow gated on a required reviewer so a visitor PR can
never reach `main`. *Recommendation: (a).* Your replay spec records what one
unguarded merge did to the replay environment; a repository that cannot
apply removes the class of accident, and a public sandbox full of visitor
PRs next to the real infra repo would also be a strange thing for a visitor
to find. Cost: one more repository and the App installed on it.

**2. Attribution on screen.** (a) "a visitor" / "you", identity only in the
audit row and CloudTrail as the Cognito user id; (b) first name from Google;
(c) full email. *Recommendation: (a).* Strangers share one incident list;
showing who else tripped the decoy is nobody's business, and it keeps the
demo out of any consent question.

**3. World size until the map at scale ships.** (a) 10,000 resources now,
accepting that the dashboard draws the first 4,000 items and says nothing;
(b) 2,500 resources (about 4,000 nodes, the current draw-whole limit) until
4.2 and 1.3 ship, then 10,000 by changing one number; (c) ship 4.2 first.
*Recommendation: (b).* The map is the first thing a visitor sees; a cut-off
map on day one undercuts the point. The knob exists either way.

**4. Cooldown.** 15 minutes per decoy (specified) versus 5 or 30. *Keep 15*:
longer than CloudTrail's delivery plus the response, shorter than a visit.

**5. Where the tour state lives.** Server-side per Cognito user (specified)
versus the browser. *Server-side*: the stand's QR code is scanned on a phone
and the laptop is where the map is worth seeing.

**6. The real account's rescan interval.** Six hours (specified) versus
hourly versus only on reset. *Six hours*: the real part changes only when we
change it; a scan costs a few minutes of the daemon.

**7. The seeded reviewed pull request.** One file, one reviewer comment,
recreated weekly (specified) versus none. *Keep it*: without it the Pull
requests tab is empty until a visitor writes, and the PR watcher's reply is
one of the three model-backed things in the product.

## Decisions taken (2026-10-21, the seven open decisions)

1. **Visitor pull requests go to `beamreach-ai/beamreach-demo-infra` itself**, not a
   sandbox repository: the workspace's `honeypot_repo` and `iac_repo` already point
   there and the pre-armed decoys were planted from it. Guard rails, in that
   repository: the `apply` job's environment gets a required reviewer so no merge
   applies without a person; visitor pull requests carry the label `demo` and
   branches `beamreach/<captain>/<slug>`, and the reset closes every open pull
   request Compass opened there. `demo.sandbox_repo` is named `demo.visitor_repo`
   in the config block.
2. **Attribution on screen is "by a visitor" / "by you"**; the Cognito user id goes in
   the audit row and in `SourceIdentity`, never an email.
3. **The world is 2,500 resources** (about 4,000 nodes, the dashboard's draw-whole
   limit) until the map at scale (4.2) and the paged query API (1.3) ship; then
   `demo.world.resources` goes to 10,000.
4. **The cooldown is 5 minutes per decoy**, shared by all visitors. Because that is
   shorter than CloudTrail delivery plus the response, the route also refuses while
   the decoy is not yet `armed` again (the re-arm after `action_verified_at`): the
   effective pacing is five minutes or until the decoy is re-armed, whichever is
   later, and the visitor lands on the open incident either way.
5. **Tour state is server-side per Cognito user** (`DEMO#TOUR#<user>`).
6. **The real account is rescanned only on reset**: `COMPASS_MAP_INTERVAL` stays 0
   and `compass demo reset` starts the scan (step 5). Nothing a visitor does is
   ever applied, so the real part does not change between resets.
7. **The seeded reviewed pull request stays**: one file, one reviewer comment from
   the bot account, recreated by every reset.

## Implementation notes (2026-10-21, as built in 0.8.0)

What the code showed that the text above assumed otherwise; `docs/demo.md` is
the reference for what shipped.

- **The synthetic world is told apart by scope, not source.** Coverage, the
  suggestions, the IaC tab and the mapper's own annotation all filter on
  `source == "aws_scan"`; a `demo_world` source would have left the world out of
  every one of them. The rows carry the collectors' sources (`aws_scan`,
  `git_scan`, `tf_state`; the generator's old `github`/`tfstate` matched nothing
  real) under the one scope `demo:seed:<n>`, and the mapper's scan lists the
  `(source, scope)` pairs it covers (it scanned the whole org before: a rescan
  would have closed the world). The synthetic repositories live under
  `acme-demo/` so no synthetic row shares an id with a real one.
- **"Trip it" is `test-fire` without the test flag.** `test-fire` already made the
  real STS call for `aws_credential` decoys; the trip is the same call under a
  user agent that marks it a trip, as the job `honeypot trip`. The pre-armed
  decoys' keys reach Compass through `honeypot arm … secret_access_key=…`
  (new), which `fire` and the trip read.
- **The re-arm and the reset's decoy work belong to the honeypot captain**
  (`captains/honeypot/demo_rearm.py`, `demo_reset.py`, the service `demo-rearm` on
  its manifest), not to the core: the core names no captain.
- **The tour's API carries facts, the dashboard decides where a step opens**: the
  step "What Terraform doesn't manage" is `unmanaged`, and `?q=`, `?token=` and
  `?incident=` on the tabs are what the panel links to.
- **One visitor in the open edition.** The cookie login has no user id, so every
  visitor is the same one there; the product's Cognito id makes trips, tours and
  "by you" per visitor.
- **The release is one, `v0.8.0`**, carrying 0.5.0 to 0.7.0 and demo mode together,
  not 0.7.0 then 0.8.0; the product bumps once.
