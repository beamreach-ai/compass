# Demo mode

Demo mode is how demo.beamreach.ai runs: this image, with `APP_MODE=demo`, a
synthetic account on the map beside a real one, and the real one's decoys to
trip. It is a feature of the open edition so that a product built on Compass
turns it on with configuration; the quickstart does not advertise it, and the
e2e suite runs it against the fakes (`e2e/test_13_demo.py`).
[docs/specs/demo-mode.md](specs/demo-mode.md) is the design.

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
            "repository": "https://github.com/beamreach-ai/compass"}
}
```

## The world: `compass demo seed`

```sh
compass demo seed                      # demo.world.resources and demo.world.seed
compass demo seed resources=2500 seed=7
```

The synthetic account is the benchmark's world (`infra_map/synthetic.py`,
[docs/benchmark.md](benchmark.md)): the mix of resource types a real account
has, four regions, a few roles and security groups carrying most of the edges,
repositories, root modules and state files covering 60% of the resources, 3%
of those drifted. At 10,000 resources it is about 16,000 nodes and 26,000
edges; the demo runs 2,500 until the map draws more than 4,000 items at once.
Deterministic from the seed.

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
it. A synthetic node's detail panel says "Synthetic resource (demo world)"
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

## The API

`GET /api/demo` (read scope) is what the dashboard asks: the mode, the world
(`resources`, `seed`, `scope`, whether it is `seeded` and how many `rows` it
holds), the map's rows per source and scope as `seed` reports them, the
`links`, the `pinned_settings`, the `cooldown_seconds` and the `trippable`
decoys. Outside demo mode it answers 404, as does every demo route. The
bootstrap (`/api/settings/bootstrap`) carries `app.demo` with the links, the
pinned settings and the world's numbers, so the dashboard marks itself on the
first request.
