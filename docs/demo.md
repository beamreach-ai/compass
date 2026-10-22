# Demo mode

Demo mode is how demo.beamreach.ai runs: this image, with `APP_MODE=demo`, a
synthetic account on the map beside a real one, and the real one's decoys to
trip. It is a feature of the open edition so that a product built on Compass
turns it on with configuration; the quickstart does not advertise it, and the
e2e suite runs it against the fakes (`e2e/test_13_demo.py`).
[docs/specs/demo-mode.md](specs/demo-mode.md) is the design.

## Run a demo, step by step

From an install that already works ([quickstart.md](quickstart.md) sections
1–3: a workspace, `connect`, one `aws_credential` decoy merged, applied and
armed). `c` below is `docker compose run --rm compass`; the workspace is
`local`, its config `./config/local.json`.

1. **Turn it on.** `APP_MODE=demo` in `.env`, then `docker compose up -d`. The
   dashboard shows the `DEMO` chip beside the account.
2. **Let Compass fire the decoy.** Store its key's secret with it, and put the
   response on autopilot if it is not yet:
   ```sh
   c honeypot arm honeytoken_id=<id> secret_access_key=<from the apply output>
   c honeypot action_set action_type=iam_deactivate_key mode=autopilot risk_max=2
   ```
3. **Say what the demo is.** Add a `demo` block to `./config/local.json` (the
   keys under [Turning it on](#turning-it-on); every one has a default). The
   least that makes "Trip it" work:
   ```json
   "demo": {"trippable": ["<id>"], "world": {"resources": 2500, "seed": 7, "edges": 20000}}
   ```
   Add `visitor_repo` (where visitors' pull requests go: a sandbox repository
   whose apply needs a reviewer), `links.slack` (an invite to the channel the
   alerts go to) and `seeded_pr` when you want them.
4. **Seed the world.** `c demo seed`: the synthetic account beside the real
   one, in about a second. The result lists the map's rows per scope.
5. **Walk it.** Open the dashboard: the tour opens bottom-right. Trip the decoy
   from step 2 and watch the incident open in one to five minutes, the key go
   inactive, and the `demo-rearm` service arm it again.
6. **Reset it.** `c demo reset` brings the golden world back (incidents
   closed, keys rolled back, visitors' pull requests closed, the world
   reseeded); `c demo status` says when it last ran. Schedule it weekly: a
   cron line on the host (`0 3 * * 0 cd /opt/compass && docker compose run
   --rm compass demo reset`) or, on AWS, an EventBridge schedule running the
   worker image with `compass demo reset`.
7. **Before strangers use it.** Put a sign-in proxy in front of it and name
   yourself in `demo.admins`: the open login is one shared account, so without
   a proxy the per-visitor limits and the tour are shared and nobody is the
   operator in the dashboard ([Who may change what](#who-may-change-what)).
   Keep `READ_ONLY` off. Check the [model budget](#the-model-budget) (60 calls
   an hour, 300 a day by default) and set a spend limit with your provider too.

To leave demo mode, remove `APP_MODE` and `docker compose up -d`; the seeded
world stays on the map, marked synthetic, under its own scope; a rescan
never touches it.

## Turning it on

`APP_MODE=demo` in `.env`. The API, the daemon and the dashboard read the same
line: the API answers the demo routes and refuses nothing (`READ_ONLY` is a
separate switch and stays off), the dashboard unlocks every tab without a
connected account and marks itself as a demo, and the `compass demo` commands
work. Outside demo mode the commands refuse in one line and the routes answer
404.

The `demo` block of the workspace config says what the demo is; every key has
a default (`compass_core/demo.py`):

```json
"demo": {
  "world": {"resources": 10000, "seed": 7},
  "trippable": [],
  "cooldown_seconds": 300,
  "trips_per_visitor_per_hour": 3,
  "visitor_repo": "",
  "seeded_pr": {},
  "reset": {"schedule": "cron(0 3 ? * SUN *)", "prune_jobs_days": 7},
  "pinned_settings": ["github", "aws", "demo", "notifications.endpoints"],
  "links": {"install": "https://beamreach.ai/compass/install.html",
            "repository": "https://github.com/beamreach-ai/compass",
            "slack": ""}
}
```

## Who may change what

A public demo lets strangers sign in, and a signed-in session has no scope (API
tokens do: [access.md](access.md)). So in demo mode the requests that change how
the demo works are the operator's alone (`compass_api.demo_guard`), answered
403 `demo_admin_only` to anyone else:

- everything an API token needs `admin` for (settings, the AWS account, GitHub,
  tokens, the decoys' defaults, the response actions, access grants);
- every write under `/api/settings`, `/api/captains`, `/api/notifications`,
  `/api/updates`, `/api/prs/`, a rescan (`/api/maps/reindex`, `/api/iac/scan`),
  `/api/iac/fix-drift`, `/api/honeypots/autopilot` and the audits' rollback;
- an incident's close, approve and respond (the incident stays for the next
  visitor; the response runs on autopilot under its grant);
- a decoy's change, delete, `arm`, runbook and placement, and its `material`
  and `lure` (the pre-armed key never leaves the server);
- forcing a new incident summary when one exists (one model call each);
- what a larger deployment adds (`extensions.register_demo_admin_prefixes`).

The tour stays open: the map, the import pull request, planting a decoy,
"Trip it", the incident, the summary there is. The operator is an address in
`demo.admins`, matched against the email the sign-in proxy vouches for
(an ALB's `x-amzn-oidc-data`, believed only when the deployment registered the
ALB's identity headers), or any API token with the admin scope. The bootstrap
says `app.demo.admin` and turns `settings_enabled` off for everyone else, so the
Settings tab is not there for them. An install without a sign-in proxy has one
shared login and so no operator in the dashboard: change it with the CLI, the
config file or an admin token.

Behind the proxy, the identity the proxy sets wins over a browser's `X-User-Id`,
so a visitor cannot pose as another to get round the per-visitor limits.

## The model budget

Every model call claims one unit of the workspace's budget first
(`compass_core.model_budget`): `limits.model_calls_per_hour` and
`limits.model_calls_per_day`, counted in the items table under
`MODEL#CALLS#H#<hour>` and `MODEL#CALLS#D#<day>` with one conditional write
each, so racing callers never pass the limit. Past it the call is refused
(`ModelBudgetExceeded`) before it reaches the provider, and each caller's "no
model" path answers instead (the decoys' deterministic plan, a template
reply). Outside demo mode an unset limit is no limit; in demo mode the defaults
are 60 an hour and 300 a day. A model the deployment builds itself says
`budgeted = True` and claims through the same function, so no call is counted
twice.

## The world: `compass demo seed`

```sh
compass demo seed                      # demo.world.resources, .seed and .edges
compass demo seed resources=2500 seed=7 edges=20000
```

The synthetic account is the benchmark's world (`infra_map/synthetic.py`,
[docs/benchmark.md](benchmark.md)): the mix of resource types a real account
has, four regions, a few roles and security groups carrying most of the edges,
repositories, root modules and state files covering 60% of the resources, 3%
of those drifted. The rules alone give about 2.6 edges per resource (10,000
resources: about 16,000 nodes and 26,000 edges); `edges` asks for at least that
many, and the generator adds more of the same kinds, the compute talking to
more queues, tables, buckets and secrets, behind more security groups, watched
by more alarms, until the count is reached (`0`: the rules alone). The demo
runs 2,500 resources with 20,000 edges: about 4,000 nodes, five edges each.
Deterministic from the seed.

The map route's `limit` is a node budget; the edges between the kept nodes all
come along. In demo mode the dashboard asks for 10,000 nodes (the world plus
the real account), elsewhere 4,000.

A larger deployment derives more from the world: `compass_core.demo.register_seeder(name, fn)`
registers `fn(config, workspace_id, world)`, run after the map is written, on
every seed and so on every reset (the product seeds its FinOps findings this
way; they are marked `source: demo-world`). The open edition registers none;
the seed's result lists each seeder's answer under `seeded`.

`seed` writes it to the map as one scan under the scope `demo:seed:<seed>`,
with the target's account id on every resource, `synthetic: true` in every
node's attributes, and its repositories under `acme-demo/`, an organisation no
real map has, so no synthetic row ever shares an id with a real one. The rows
carry the mapper's own sources (`aws_scan`, `git_scan`, `tf_state`), so
coverage, drift, suggestions and the detail panel treat them as resources.

Beside it, the real account is scanned as always. A scan closes only the rows
within the `(source, scope)` pairs it covers, and the mapper lists every pair
it produced plus every pair already on the map except the `demo:` ones
([map-store.md](specs/map-store.md) §3.4), so a rescan never touches the
world. Seeding again with the same numbers is a scan in which every row is
unchanged (`added 0, changed 0, removed 0`); other numbers close the old world
and write the new one. The result lists the map's rows per source and scope,
synthetic ones marked:

```json
{"scope": "demo:seed:7", "resources": 2500, "nodes": 4030, "edges": 6561,
 "scan": {"status": "completed", "added": 10591, "changed": 0, "removed": 0},
 "closed_scopes": [],
 "map": [{"source": "aws_scan", "scope": "demo:seed:7", "rows": 6231, "synthetic": true}, …,
         {"source": "aws_scan", "scope": "aws:full", "rows": 112, "synthetic": false}, …]}
```

The seed needs the target account (`aws.account_id`, set by `compass connect`)
and runs on whichever store the workspace names, SQLite or Postgres.

## Marked as a demo

Nobody mistakes the demo for their own account. In demo mode the dashboard
shows a `DEMO` chip beside the target account in the sidebar and in the
phone's top bar, the page title reads `Compass · demo`, and the map canvas
and every exported map page carry a `DEMO` watermark in the lower right,
drawn over the graph so it is in the pixels of a screenshot. None of these
can be dismissed. One banner says what the demo is ("Demo account. Plant,
open pull requests, trip a decoy and watch the response: it all runs against
a shared demo account that resets on Sunday.") and can be dismissed for the
session; the read-only notice is a separate thing and shows only when
`READ_ONLY` is also on. The sidebar's "Run it yourself" goes to
`demo.links.install` (else `PRODUCTION_URL`), with "Source on GitHub" under
it, and "See the alerts in Slack" when `demo.links.slack` is set: an invite
link to the channel the workspace's Slack endpoint posts to. The tour's
"Watch the response" step shows the same link. Empty hides both. A synthetic node's detail panel says "Synthetic resource (demo world)"
under its name.

Writes a visitor makes are attributed "by you" or "by a visitor", never a
name or an email: the pull request list has a By column in demo mode, from
the requester a job recorded on the pull request's origin. The settings the
demo fixes (`demo.pinned_settings`: the AWS access, GitHub, the demo block,
the notification endpoints) are read-only with "Fixed in the demo" on them.

## Trip it

The only decoys a visitor fires are the pre-armed ones, `demo.trippable`:
`aws_credential` decoys whose key Compass holds (`honeypot arm
honeytoken_id=… secret_access_key=…` stores the secret from the apply output;
the API's `arm` takes it in the body). `POST /api/honeypots/tokens/{id}/trip`
(demo mode only, 404 otherwise; `operate`) refuses with the reason (409) when
the decoy is not trippable, not `armed`, or has no stored key, and otherwise:

- **Cooldown.** One trip per decoy per `demo.cooldown_seconds` (300), shared
  by every visitor, kept as the row `DEMO#COOLDOWN#<token>` and claimed with a
  conditional write, so two visitors pressing at once get one trip. During
  it the route answers `{"state": "cooldown", …}` with the trip's `incident_id`
  and `seconds_left`, and the dashboard takes the visitor to that incident:
  "Tripped 2 minutes ago by a visitor; the incident is open". A visitor gets
  `trips_per_visitor_per_hour` (3); past it, 429.
- **The fire** is the job `honeypot trip`, attributed to the visitor (the
  job's requester, so the act sessions it causes carry the id): one STS call
  with the stored key, under a user agent that marks it a trip and not a
  test, then the wait for CloudTrail (one to five minutes) and the alert that
  opens the incident. The job's events and the cooldown row carry `fired_at`,
  `alert_received_at` and `incident_id`; `GET /api/demo` lists every decoy's
  last trip as `cooldowns` (never the visitor's identity, only "you" or "a
  visitor").
- **Then the real path**: the queue, the consumer, the incident, the
  investigation, the gate, the response (`ecs_restart_task`,
  `iam_deactivate_key` on autopilot).
- **Re-arm.** The core service `demo-rearm` (every 30 s, `COMPASS_DEMO_REARM`;
  it does nothing outside demo mode) finds the trips whose incident has
  `action_verified_at`, rolls back each `iam_deactivate_key` audit not yet
  rolled back through the act role under its grant (the same rollback the
  incident page offers), and runs `arm` for the decoy. The incident stays open
  for the next visitor to read; a failed re-arm is logged and written on the
  row as `rearm_error`, and the next tick tries again.

`test-fire` answers 404 in demo mode: it would be a second button for the
same story. The token card shows "Trip it", the countdown while a trip is in
force, and the link to the incident; `?incident=<id>` on the Honeypots tab
opens one.

## The tour

A panel bottom-right (`TourPanel`), collapsible to a pill ("Tour · 3 of 7"),
opened by itself on a visitor's first visit and resumable from the pill. Its
steps are facts about the world, read from `GET /api/demo/tour` every ten
seconds while the panel is open; a client-only step posts itself
(`POST /api/demo/tour/{step}`), "Skip" is `POST /api/demo/tour/{step}/skip`.
State is one row per visitor, `DEMO#TOUR#<user>`, so a visitor who scanned the
stand's QR code on a phone resumes on a laptop.

| # | Step | Opens | Done when |
|---|---|---|---|
| 1 | See the account | Maps, `?q=payments` (the box filled, the first hit selected) | a node's detail opened (client) |
| 2 | What Terraform doesn't manage | IaC | the IaC tab viewed (client) |
| 3 | Open an import pull request | IaC, the search filled with `demo.tour.import_resource` | a pull request by this visitor exists (`origin.command == import`, the job's requester) |
| 4 | Meet the decoys | Decoys → honeytokens, `?token=<first trippable>` expanded | a trippable decoy's detail opened (client) |
| 5 | Trip one | the same, "Trip it" | this visitor's trip fired, or the trip in force was shown to them with its incident (the token card posts it) |
| 6 | Watch the response | the incident (`?incident=<id>`) | `action_verified_at` on that incident |
| 7 | Run it yourself | a card with the install page and the repository | a link clicked (client) |

Each step has a one-line "why this matters" and a "Skip"; a skipped step
counts towards the end but not as done. The last step's card is the demo's
one call to action. The open edition has one login and no user id, so there
every visitor is the same one; the product's Cognito id makes the tour per
visitor.

## Reset and status

`compass demo reset` brings the golden world back, idempotent, in this order,
each step counted in the result and in the row `DEMO#RESET#<ts>` it writes:

1. **The decoys** (the honeypot captain's command `demo_reset`): every open
   incident closed with `resolution: demo reset`; every verified
   `iam_deactivate_key` not yet rolled back rolled back through the act role
   under its grant; the pre-armed decoys armed again; the ECS service their
   placement names waited for until it runs what it wants (five minutes at
   most). What `demo/replay/run.sh reset` did by hand.
2. **The visitors' pull requests**: every open pull request Compass opened on
   `demo.visitor_repo` closed on GitHub, its branch deleted, its record marked
   closed (`demo reset`), the decoy it planted retired in place
   (`decommissioned`, no decommission pull request) and the grants whose pull
   request it was disarmed with no revoke pull request.
3. **The seeded pull request** recreated: one fixed file change
   (`demo.seeded_pr.path`, default `services/payments/variables.tf`) on the
   branch `beamreach/demo/payments-variables`, labelled `demo`, and one review
   requesting changes from the bot account (`demo.seeded_pr.reviewer_token`;
   without one there is no review, since GitHub refuses a review requesting
   changes from the pull request's own author, and the result says so),
   registered so the PR watcher lists it under Needs you.
4. **Jobs** older than `demo.reset.prune_jobs_days` (7) pruned; every
   `DEMO#COOLDOWN#`, `DEMO#VISITOR#` and `DEMO#TOUR#` row cleared.
5. **The world** seeded again (a no-op scan when unchanged) and a real rescan
   started (the mapper's job).
6. One line with the counts to every Slack endpoint the workspace has.

The deployment schedules it (`demo.reset.schedule`, `cron(0 3 ? * SUN *)`,
EventBridge Scheduler → ECS RunTask of the worker image with `compass demo
reset`); it runs by hand any time. A second run right after changes nothing
but the seeded pull request, which it closes and recreates.

`compass demo status` prints the last reset (its counts), the next one from
the schedule, the trips in force, the visitors' open pull requests and the
tours that reached the end this week.

## The API

`GET /api/demo` (read scope) is what the dashboard asks: the mode, the world
(`resources`, `seed`, `scope`, whether it is `seeded` and how many `rows` it
holds), the map's rows per source and scope as `seed` reports them, the
`links`, the `pinned_settings`, the `cooldown_seconds` and the `trippable`
decoys. Outside demo mode it answers 404, as does every demo route. The
bootstrap (`/api/settings/bootstrap`) carries `app.demo` with the links, the
pinned settings and the world's numbers, so the dashboard marks itself on the
first request.
