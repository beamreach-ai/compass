# Changelog

One section per release, newest first: `## <version> — <date>`. The release
workflow refuses a tag without one, and reads from it what the dashboard shows
next to the Update button: the first paragraph is the summary, the bullets under
`### After the update` are the notes an operator needs after applying it.

## 0.9.0 — unreleased

The inbox: everything that needs a person, as one list. `GET /api/inbox` derives
it from state on every call (a parked decoy response, a grant to accept, a
decoy waiting for its merge or its arm, a pull request with a revision to
approve or that the watcher gave up on, a failed job, an access finding, an
available update, an incomplete setup), newest first, each item carrying the
routes that act on it. Nothing is stored and nothing is marked read: an item
leaves when the state changes. [docs/inbox.md](docs/inbox.md).

- `GET /api/inbox` with `tab=`, `kind=`, `verb=`, `include_snoozed=`; counts per
  tab and per verb. `POST`/`DELETE /api/inbox/{id}/snooze` for the three kinds
  with a natural end (a failed job until it fails again, an update until the
  next release, "no model" on purpose). `/api/status` gains an `inbox` section
  with the counts and the line `compass status` prints.
- In the dashboard: a count beside every tab that has something, the total
  beside the brand that opens the whole feed, a "Needs you" view on the Decoys,
  Pull requests and Queue tabs, Settings opening with its items, and inline
  actions (Run, Accept, Retry, Arm, Approve the revision, Acknowledge) with the
  confirmation before anything that changes the account or a pull request.
- `POST /api/honeypots/incidents/{id}/decline` drops one parked response and
  records who decided against it; the incident stays open.
- The map knows what every workload is made of, as package URLs on the node
  (`attributes.software`): the image of each container in a task definition,
  resolved to its digest through ECR; a Lambda's runtime and layers (Bref's
  by name) and its function URL; an instance's AMI, its public address and,
  when SSM Inventory is on, its packages; a security group's rules open to
  the world. In the repositories the mapper now reads Dockerfiles fully
  (`FROM` per stage, `apt-get install`, `apk add`, `pip install`, `composer
  require`), manifests and lockfiles (composer, npm, yarn, pnpm, pip,
  pyproject, uv, poetry, go, cargo), `serverless.yml`, SAM templates and the
  runtimes, layers and AMIs in Terraform. Every workload carries `exposed`,
  with how (an internet-facing load balancer, a function URL, a public IP
  behind an open security group). The patching captain matches these; the
  mapper's read role gains `ssm:ListInventoryEntries`.
- A new captain, `patch` (Patches): the `cve-feed` service follows the CVE
  feeds every hour without a key or an account. OSV is asked by the purls on
  the map on every run, so a fresh install is current in minutes and new
  software is covered as soon as the mapper sees it; cvelistV5's delta log
  brings the CVEs of the products the account runs (PHP, Node, Python, nginx,
  Apache, Ruby, Go, Redis, Postgres, MySQL, MariaDB and friends); CISA's KEV
  and FIRST's EPSS score them; the Ubuntu, Debian, Alpine and Amazon Linux
  trackers say which package version in which release is fixed; Packagist and
  npm add the PHP and JS advisories at the source. Advisories merge on the CVE
  id and live in the items table (`CVE#`); each source keeps its own cursor,
  one failing never stops the others. `compass patch feed_run`, `feed_status`,
  `advisories` and `inventory` read it; `feed_inject` is a test-only door
  behind `COMPASS_PATCH_INJECT=1`.
- The Patches tab and the match behind it: every fifteen minutes the
  advisories are held against the map's software in each ecosystem's own
  version order, and a finding is written per advisory, node and package with
  the fixed version, the repository and file, the workloads that run what the
  file builds and whether they face the internet. The policy decides per
  finding: a pull request without a click when exploited, likely to be, severe
  or exposed, and nothing stands in the way of a plain bump; an inbox item per
  repository otherwise, approved with one click. `GET /api/patches`,
  `compass patch findings|tick|approve|ignore`. [docs/patching.md](docs/patching.md).
- The fix pull requests: one per repository with every fix it needs, each a
  version string moved to the one the advisory names (a docker-library tag, an
  apt or apk pin, a layer version, a direct dependency's constraint), the body
  with the findings, the changes, the verification and the rollback, labels,
  the PR watcher keeping it alive, and the findings following the pull request
  from `pr_open` to `merged` to `deployed`. `compass patch pr`,
  `POST /api/patches/pr`.
- A captain contributes items through `inbox=` in its manifest
  (`compass_core.inbox`, [docs/writing_a_captain.md](docs/writing_a_captain.md) §6.1);
  a product registers a source with `compass_api.extensions.register_inbox_source`.

## 0.8.0 — 2026-10-21

Demo mode: with `APP_MODE=demo` this image runs a public demo the way
demo.beamreach.ai does, a synthetic account on the map beside a real one.
[docs/demo.md](docs/demo.md) says how. The quickstart does not change. This
release also carries 0.5.0 to 0.7.0, which were not released on their own:
read their sections below, and their "After the update" notes apply to an
install coming from 0.4.1.

- `compass demo seed [resources=…] [seed=…]` writes the benchmark's synthetic
  world (`infra_map/synthetic.py`, until now `tests/benchmark/world.py`) to the
  map as one scan under the scope `demo:seed:<n>`, next to the real account's
  scan, with `synthetic: true` on every node. The mapper's scans now list the
  `(source, scope)` pairs they cover (every pair they produced and every pair
  already on the map, except the seeded world's), so a rescan closes what it no
  longer sees and leaves the world alone. Seeding the same numbers again changes
  nothing; other numbers replace the world. Demo mode only.
- The map store answers `scopes(org_id)`: the `(source, scope)` pairs with
  current rows and their counts, on SQLite, Postgres and DynamoDB.
- Demo mode is marked: a `DEMO` chip beside the account and in the phone's
  top bar, `Compass · demo` as the title, a watermark on the map canvas and on
  exported map pages, one dismissable banner saying what the demo is, "Run it
  yourself" to the install page, "by you" / "by a visitor" on pull requests,
  "Fixed in the demo" on the settings the demo pins, and "Synthetic resource
  (demo world)" on a synthetic node's detail. The read-only notice stays its
  own thing (`READ_ONLY`). `APP_MODE` reaches the dashboard from `.env`.
- Trip it: `POST /api/honeypots/tokens/{id}/trip` (demo mode only) fires one of
  the pre-armed decoys (`demo.trippable`) for real, as the job `honeypot trip`
  attributed to the visitor, under a shared cooldown per decoy
  (`demo.cooldown_seconds`) and a per-visitor limit; a visitor in the cooldown
  is sent to the incident the trip opened. The core service `demo-rearm` rolls
  the key deactivation back once the response verified and arms the decoy
  again. `test-fire` is hidden in demo mode. `honeypot arm` and the arm route
  take `secret_access_key` and keep it with the token, so Compass can use the
  key itself.
- The tour: seven steps in a panel bottom-right, each completed by a fact
  about the world (`GET /api/demo/tour`, per visitor), opened on the first
  visit and resumable from a pill; `?q=` fills the map's and the IaC tab's
  search, `?token=` opens a decoy's detail, `?incident=` an incident.
- `compass demo reset` brings the golden world back: incidents closed, key
  deactivations rolled back, the pre-armed decoys armed again, the visitors'
  pull requests closed and their decoys and grants retired without a pull
  request, the seeded reviewed pull request recreated, old jobs pruned, the
  trips and tours cleared, the world seeded again and a rescan started, with
  a row of counts and one line to Slack. `compass demo status` prints the
  last reset, the next one, the trips in force, the open visitor pull
  requests and this week's tour completions.
- Demo mode keeps the operator's settings to the operator: a signed-in
  visitor is refused (403 `demo_admin_only`) what an admin token needs, every
  settings write, rescans, PR actions, incident close and respond, a decoy's
  arm, change and `material`, and a forced summary; the tour stays open. The
  operator is `demo.admins` (the email the sign-in proxy vouches for) or an
  admin API token; the Settings tab is hidden from everyone else. The identity
  a proxy sets now wins over a browser's `X-User-Id`.
- A model budget: every model call claims from `limits.model_calls_per_hour` and
  `limits.model_calls_per_day` (60 and 300 by default in demo mode, none
  otherwise) with a conditional write, and is refused past it before it
  reaches the provider (`compass_core.model_budget`).
- `demo.links.slack`: an invite link to the channel the alerts go to; when
  set, the sidebar shows "See the alerts in Slack" and the tour's response
  step links it.
- `demo.world.edges` (default 20,000; `compass demo seed edges=…`): the world
  has at least that many edges, more of the kinds the rules already produce,
  deterministic from the seed. `compass_core.demo.register_seeder(name, fn)`
  runs `fn(config, workspace_id, world)` after every seed, for what a larger
  deployment derives from the world; the open edition registers none.
- Fixed: an alert queue recorded by its ARN alone gets its URL from the ARN;
  `arm` asked SQS for an empty URL and refused the decoy.
- Fixed: the 0.7.0 access-model migration kept one external id and dropped
  the target's region. An install whose read and act roles trusted different
  external ids now keeps the act role's as `aws.external_ids.act` (the broker
  uses a role's own id first), and the old blocks' `region` becomes
  `aws.region`. An install already migrated sets both by hand.
- Fixed: the map store's `fetch_graph(limit)` spent the budget on nodes first,
  so a map just under the dashboard's 4,000 came back with every node and no
  edge. The limit is a node budget and the edges between the kept nodes all
  come along, on SQLite, Postgres and DynamoDB. In demo mode the dashboard asks
  for 10,000 nodes.
- Fixed: the grant verification sends a statement's own condition to IAM's
  simulation as the request's context. Without it IAM answered `implicitDeny`
  for every conditioned statement, so `ecs_restart_task` (its statements carry
  `ArnEquals ecs:cluster`) stayed `applied` forever on a real account;
  `iam_deactivate_key` was never affected.

## 0.7.0 — 2026-10-21

Compass only reads your account, through a role it creates once, and writes
nothing without a pull request you merge. The operator's profile is used by
one command, `compass connect`; the running services never see it.

- `compass connect account=<id> region=<r>`, run once with your profile,
  creates in the account a read role whose policy is generated from what the
  installed captains declare they read, an act role with no permissions, the
  queue decoy alerts report to (in the control account, SQS-managed
  encryption), and, off AWS, a runtime user whose only right is to assume the
  two roles. On AWS it trusts the machine's own role instead. `connect verify`,
  `connect rotate` and `connect template` (CloudFormation and Terraform for an
  account out of reach) complete it.
- Every captain's manifest declares what it reads and the write actions it may
  propose (`permissions`); `compass validate` fails on a call the manifest does
  not cover or an action it does not register, and the Captains page shows the
  block as "Will use" before anything is installed. Each session is
  narrowed to that declaration, so a captain cannot read more than it declared
  whatever the role allows. `lambda:Get*` is never a read: it downloads code.
- Every AWS session Compass makes comes from one broker, `compass_core.sessions`,
  and is named `compass-<captain>-<job>` with `SourceIdentity` set to who asked,
  so the target account's CloudTrail says which captain, which job, and whom.
- A response action is a grant: allowing it opens the pull request that gives
  the act role exactly that permission, and it runs only once the pull request
  is merged, applied and verified against IAM (`proposed → pr_open → merged →
  applied → verified → armed`). Removing it disarms it at once and opens the
  pull request that takes the permission away. Settings → Account → Access
  shows every grant with its state and pull request; `compass access` lists,
  accepts, removes and verifies them. Each action runs in its own session,
  narrowed to that grant and attributed to the person who approved it.
- Compass audits its own access every day and on demand (`compass access
  audit`, Settings → Account → Access): both roles' trust, inline and attached
  policies against what the manifests, the grants and the config explain. A
  statement added in the console, an attached managed policy, a second
  trusted principal, a grant that says armed but is not in the role, or a
  role that no connected repository holds is a finding, with the time it was
  first seen; the access pull request is its fix.
- The services refuse to start with `AWS_PROFILE` set or `~/.aws` mounted, or
  as anything but the runtime identity `connect` recorded.
- `local_iam` and static keys are gone as ways in; `init_alerts` is absorbed
  into `connect`. The `aws` block of the config names roles, not credentials.
- The 0.6.0 map migration copies the DynamoDB map into the store the workspace
  names (`map.store`, or `COMPASS_MAP_STORE`; Postgres for a deployment that set
  it) and keeps that setting, instead of always writing a SQLite file and
  pointing the workspace at it. `compass migrate rerun=<version>[,…]` runs a
  recorded step again, and every step gets the workspace id whatever loader
  gave it the config.

### After the update

- Run `docker compose run --rm -v ~/.aws:/root/.aws:ro -e AWS_PROFILE=<profile>
  compass connect account=<id> region=<r>` once, then `docker compose up -d`.
  Remove `AWS_PROFILE` from `.env` and the `~/.aws` mount from an edited
  compose file. An install that assumed a role keeps it as the read role; one
  that used the profile runs nothing against the account until `connect` has run.
- Decoys planted before this release report to the old queue until
  `honeypot replant-alerts` has opened and you have merged its pull request.

## 0.6.0 — 2026-10-21

The map moves from DynamoDB to a SQLite file and keeps its history: scans of a
100,000-resource account take seconds instead of the better part of an hour,
and the map reads that took tens of seconds take milliseconds.

- The map is one SQLite file per workspace on a new `map-data` volume
  (`/compass/data/map/<workspace>.sqlite`). The daemon writes a scan in one
  transaction; the API reads at the same time and never sees half a scan.
  DynamoDB keeps the jobs, decoys, pull requests and the rest.
- A scan no longer deletes what it did not find: it closes it. Every node and
  edge is a version with the time it appeared and the time it went, so the map
  can later be read as it was and two scans compared (the API for that comes
  in a later release).
- Every reader of the map goes through one interface, `GraphStore`
  (`infra_map.graph_store`), with `set_graph_store_factory` for a deployment
  that brings its own store. A Postgres store implements it too:
  `pip install compass-open[postgres]` and `map: {store: postgres, dsn: …}`.
- `compass mapper backup out=<path>` copies the map while the stack runs.
- The map benchmark (`docs/benchmark.md`): a synthetic account of 10k, 100k or
  1M resources, run against each store. 10k runs on every pull request with
  its budgets enforced; 100k nightly.
- A neighbourhood around a resource with thousands of edges (a shared role, a
  security group) is bounded and returns; on DynamoDB its edge listing was cut
  at 1 MB without saying so.

### After the update

- Fetch the new `docker-compose.yml` once: it adds the `map-data` volume.
- The first start copies the existing map from DynamoDB into the file, once
  (`compass migrate`, step `0.6.0-map-sqlite`). On a large map the first start
  takes a few minutes longer. The DynamoDB table is left as it was.
- To go back to the DynamoDB map, set `COMPASS_MAP_STORE=dynamodb` in `.env`
  and restart; the next scan brings the table up to date.

## 0.5.2 — 2026-10-21

An end-to-end suite that runs on every pull request, and the bugs it found on
its first runs.

- `pytest e2e` (docs/e2e.md): the stack built from the commit, the shipped
  compose file and the quickstart's commands, against a fake AWS account (moto)
  and a fake GitHub, driven through a real browser, the API and the `compass`
  command. 75 tests over install, sign-in and tokens, the dashboard, settings,
  captains, the map, decoys, code-as-cloud, the PR watcher, jobs, updates and the
  release artefacts. Model-free; no account or token needed.
- In CI the suite runs in four parallel shards from images built once into
  GitHub's build cache. A pull request runs what its change needs
  (`.github/scripts/e2e_plan.py`): nothing for docs, the CLI or unit tests; one
  captain's tests and the smoke tests for a change to that captain; everything
  for shared code. The real compose update runs only when the images, the
  compose file or the update code change. Everything runs nightly on `main` and
  before a release is published. The required check is `e2e / e2e`.
- `GITHUB_API_URL` and `GITHUB_GIT_URL`: where Compass talks to GitHub, for
  GitHub Enterprise Server (`https://<host>/api/v3`; the clone URL follows) and
  for the suite's fake. Unset, nothing changes.
- The alert consumer starts with the workspace config as it is now. The
  quickstart runs `init_alerts` after `up -d`, and the daemon kept the config
  it read at start, so decoy alerts were not consumed until it was restarted.
- A pull request on a host other than github.com is tracked by the watcher (a
  GitHub Enterprise Server install's were not).
- `compass init_config` on an existing workspace prints the one line telling
  you to pass `overwrite=true` and exits 2, instead of a traceback.
- `--workspace` with an id that is not the config file's (a typo) is refused,
  instead of quietly acting on the workspace in `COMPASS_CONFIG`.
- Planting an `s3_object` decoy without a bucket is refused with what to
  enter, and a `dns_record` decoy without a zone uses the default zone; both
  crashed the plant job. A suggestion's ARN is accepted as the bucket or zone,
  so autopilot plants S3 and DNS decoys in the right place.
- Fix-drift that could not make its pull request (no model, a refused push)
  fails with the reason, instead of completing with nothing done.
- The page's "Check now" for updates reaches the daemon (they share the
  check on the extensions volume), so an automatic patch update does not wait
  for the daemon's own six-hourly check.
- The plant form's fields are labelled for screen readers; the Maps panel and
  the banner say "re-scan" for a scan after the first one.
- The decoy settings describe `network` (VPC and subnets), which a
  `db_credential` decoy's listener needs.
- The API writes the jobs it starts to the jobs table again. Since 0.4.1 it
  took the inline queue the CLI installs before `compass api` for a host's
  queue, so every job started from the dashboard ran inside the request, and
  those of other captains failed.
- The updater tries `docker compose up` once more before it calls an update
  failed: CI saw Compose itself crash once, which is no reason to roll back.
- `compass status` in the CLI says when a release is out and when the compose
  file asks for another version than runs; the status skill mentions it.
  The API version stays 2026.10.1: the additions do not need a CLI that
  knows them, and a bump would have made every CLI call 0.5 incompatible.
- Settings → Updates shows **What's new** when a release is out: the release
  notes of every version between the one you run and the one on offer,
  newest first, read from the RSS feed on beamreach.ai on the check's
  schedule. The banner links to it, and `compass status` lists each release's
  headline. `COMPASS_RELEASE_NOTES_URL` points at a mirror or a file; `off`
  stops just the notes. Notes that cannot be read do not hold up the update.

## 0.5.1 — 2026-10-21

Dashboard polish: readable buttons, Settings in sections, a progress bar for
map scans.

- Every blue button is the same blue with white text. Buttons that named only
  a text colour (Refresh map on the Maps tab, and others) came out as grey or
  blue text on blue, and turned pale grey under the pointer.
- Settings sits at the bottom of the sidebar. While it is open it lists its
  sections, Integrations, Captains and Updates; each scrolls to its part of
  the one Settings page.
- A captain is turned on or off with a switch, and a click anywhere on its row
  opens its configuration.
- A running map scan shows a progress bar on every tab, with the phase, the
  region it is reading, the resources found so far, the time it has run and
  about how long is left. On the Maps tab it is in the scan panel. Re-scans
  show it too, not only the first scan. The mapper's region events carry the
  region, its position and the count so far.
- `compass serve` records a job's progress events on the job in the jobs
  table, not only in its log. Before, the dashboard never saw a scan's progress
  in the open edition and its bar stayed at the start.
- The job list returns the newest jobs. It read one page of the jobs table in
  key order, which is not time order, so once a workspace had more jobs than
  the page (the PR watcher adds one every five minutes) the Queue tab and the
  progress bar could miss the job that was running.

## 0.5.0 — 2026-10-21

Updates: the dashboard says when a release is out and applies it with one
click when the updater service runs; `compass update check | apply | rollback`
from the shell; `compass migrate` on every new version.

- The Compass CLI and MCP server (`cli/`, `@beamreach/compass`) move into this
  repository, with a plugin interface. They landed after the 0.4.1 tag; the
  0.4.1 entry used to claim them.
- `releases.json` on the `releases` branch, written by the release workflow,
  with the images' digests, what each release upgrades from directly, and the
  notes from this file. A GitHub Release with the same notes.
- The check: one anonymous request for that file every six hours, with the
  version in the user agent. `COMPASS_UPDATE_CHECK=off` turns it off;
  `COMPASS_UPDATE_CHANNEL=prerelease` follows release candidates.
- A banner above the tabs and a Compass card on Settings: running and pinned
  version, channel, last check, the update policy, Update or the two commands,
  Roll back for seven days after an update.
- The updater service (`docker compose --profile updater up -d`): pulls by
  tag, verifies by digest, waits for running jobs, recreates the services,
  checks health, rolls back when they do not come up. Off by default; it
  holds the Docker socket.
- `COMPASS_AUTO_UPDATE=patch|minor` and `COMPASS_UPDATE_WINDOW` let the
  daemon ask the updater on its own; the default `notify` never applies
  anything without a click.
- `compass migrate`: missing tables and the migration steps a version
  registers, once per workspace; the entrypoint runs it for `serve` and
  `api`. Extensions are rebuilt when a new core runs.
- The version the API reports is the code's (`compass_core.__version__`), not
  `COMPASS_VERSION`; the page says when the two disagree.
- The compose file's default version follows the latest release; the release
  workflow commits the bump.

### After the update

- Fetch the new `docker-compose.yml` once: it carries the updater service and
  passes `COMPASS_VERSION` into the containers.

## 0.4.1 — 2026-10-16

Hosts: a product built on Compass installs its seams before the open
defaults.

## 0.4.0 — 2026-10-15

Install, update and uninstall captains from the page; restart Compass from
the page; `compass validate`; the development guide.

## 0.3.0 — 2026-10-14

The captain catalog (Settings → Captains → Available) and the example captain.

## 0.2.1 — 2026-10-14

Captains as extensions; tests import by path.

## 0.2.0 — 2026-10-14

Captains as extensions: the registry, the Captains page, extensions installed
into the image.

## 0.1.0 — 2026-10-13

Compass, the open edition: the infra map, decoys, code-as-cloud, the PR
watcher and the dashboard.
