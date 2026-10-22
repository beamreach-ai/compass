# Architecture

Compass is three processes, a database and a file, all on your machine, with
your AWS account and your GitHub repository as the only things outside.

```
 browser ──► nginx :8080 ──┬── /      ──► dashboard (Next.js)
                           ├── /api/  ──► api       (FastAPI: compass_api)
                           └── /map/  ──► compass   (the daemon's map viewer)
                                           │
                 compass serve (the daemon) ┤  runs jobs, consumes decoy alerts,
                                           │  ticks the PR watcher, re-scans
                                           ▼
                                   DynamoDB (local) ◄── api    jobs, decoys, state
                                   the map (SQLite) ◄── api    one file on the map-data volume
                                           │
        AWS account (read, through a role Compass narrows per captain;
         writes only through an action a merged pull request granted) · GitHub (pull requests)
```

Everything the dashboard starts — a scan, a decoy, an import — is a **job**:
a row in the jobs table that the daemon claims, runs, and marks done. The API
never waits on work. That is the whole queue; there is no broker.

## Packages

| Package | What it is |
|---|---|
| `compass_core` | What every captain shares: the config file, DynamoDB table names and keys, the AWS session, the GitHub client, the job store and queue, events, the model, the `compass` command. The registries (`dispatch`, `queue`, `config`, `llm`, `notify`, `demo.register_seeder`) have a default and a hook, so a larger deployment can plug in behind the same code. |
| `infra_map` | The map: resource and ownership nodes, edges, and the `GraphStore` they live in (`graph_store.py`: the interface and `open_store`; `sql_store.py`: SQLite, the default, and Postgres; `dynamo_store.py`: the DynamoDB map of installs before 0.6). The mapper writes it, one scan at a time; everything else reads it. |
| `captains/mapper` | Reads the AWS account (every region), Terraform state and the GitHub organisation, and writes the map: which resource is managed by which module in which repository. `export` renders it as JSON, DOT or one HTML page. |
| `captains/librarian` | Answers questions against the map ("what owns this ARN", "what is unmanaged") for the API and the other captains. |
| `captains/honeypot` | Decoys: classes (`aws_credential`, `db_credential`, `ssh_key`, `s3_object`, `api_token`, `dns_record`), placement (where a decoy would look real, from the map), the Terraform generator, the pull request, arming, the alert consumer, incidents, blast radius, and the responses (`iam_deactivate_key`, `ecs_restart_task`). |
| `captains/iac` | Code-as-cloud: import unmanaged resources into Terraform (generate HCL, rewire literals to references, verify with a plan, open the pull request) and fix drift. |
| `captains/prs` | The PR watcher: syncs the pull requests Compass opened, reads reviews, answers, revises within a risk policy (`claude_runner.py`: Claude edits the checkout through four tools, the intent guard checks the diff, a plain push adds one commit), labels what needs a human. |
| `compass_api` | The FastAPI app behind the dashboard: sign-in and API tokens, jobs, target, maps, honeypots, incidents, notifications, Slack, pull requests, settings. One workspace whose config is a file. |
| `docker/updater` | The opt-in updater: `docker:cli` plus one shell script that applies a requested release and rolls it back when health fails. No Compass code. |
| `dashboard` | Next.js 14. Tabs: Maps, Honeypots, IaC, Pull requests, Queue, Settings. `app/components/DashboardClient.tsx` is the shell; `app/lib/shell.tsx` is what it exposes to plugged-in modules; `app/lib/extensions.ts` is the registry (tabs, settings sections, banners, providers) a larger deployment fills — empty here. |

## Vocabulary

- **Captain** — a package that does one kind of work (`mapper`, `honeypot`,
  `iac`, `prs`, `librarian`). **Command** — one thing a captain does, a
  function of a payload dict returning a dict, registered in the captain's
  `commands.py`. The CLI, the API and the daemon all go through
  `compass_core.dispatch`.
- **Workspace** — one install's state: a config file and a set of DynamoDB
  tables named `compass_<table>_<workspace>`. **Target** — the AWS account the
  workspace maps.
- **Decoy / honeytoken** — a credential or object with no legitimate use,
  placed where an attacker would find it; using it is the alarm.
  **Placement** — where it sits and what it pretends to be. **Incident** —
  one trip of one honeytoken, with its blast radius and the responses taken.
- **Job** — one run of one command, with its events; the daemon is the only
  thing that runs jobs.

## Storage

**The map** is a SQLite file per workspace,
`/compass/data/map/<workspace>.sqlite` on the `map-data` volume
(`COMPASS_DATA_DIR`; `./data` in a checkout). The daemon writes it, one scan per
transaction; the API reads it at the same time (WAL). Every node and edge is a
version with `valid_from` and `valid_to`: a scan closes what it no longer sees
instead of deleting it. `map.store` in the workspace config names the store
(`sqlite`; `dynamodb` for an install not yet migrated); `COMPASS_MAP_STORE`
overrides it for one process. [docs/specs/map-store.md](specs/map-store.md) is
the design, [docs/benchmark.md](benchmark.md) the numbers.

**Demo mode** (`APP_MODE=demo`, [docs/demo.md](demo.md)) puts a synthetic
account on the same map: `compass demo seed` writes the benchmark's world
(`infra_map/synthetic.py`) as one scan under the scope `demo:seed:<n>`, and the
mapper's scans list their own scopes, so a real rescan never closes it.
`compass_core/demo.py` holds the commands and the `demo` config block.

**Everything else** is DynamoDB, one table set per workspace (`init_tables`
creates them): `items`, `aws_inventory`, `tf_inventory`, `tf_state`,
`progress`, `jobs`, and `map`, which holds the fact store and, on an install
from before 0.6, the old map (`compass migrate` copied it to the file and left
it in place for a rollback). Keys are built in `compass_core.keys` and nowhere
else. Locally the tables live in the `dynamodb-data` volume; point
`AWS_ENDPOINT_URL_DYNAMODB` elsewhere to use a real one.

The config file (`config/local.json`) is the workspace: the target account
and the two roles `connect` created, the runtime identity and its key, the
alert queues, the GitHub token and repositories, PR-watcher policy, dashboard
sign-in. `compass_core.config` reads it; the API writes it back when you change
settings.

## Access

Compass only reads a target account, and only what each captain declared. The
operator's profile is used once, by `compass connect`, which creates two roles
in the account: a **read role** whose policy is generated from the installed
captains' manifests (`compass_core.permissions`), and an **act role** that
starts with no permissions. The services run as a **base identity** that may
only assume those roles: the machine's own role on AWS, elsewhere a runtime
user `connect` creates whose key lives in the config. Every AWS session comes
from one broker, `compass_core.sessions`: `control()` for the control side
(DynamoDB, the alert queues, SES), `target("read")` or `target("act")` for the
account, each named `compass-<captain>-<job>`, narrowed by a session policy to
the captain's declaration, and carrying who asked as `SourceIdentity`. A
response action gets its permission by a pull request a person merges, and a
scheduled audit flags anything in either role that no manifest or grant
explains. [docs/access.md](access.md) has the whole model.

## Where the seams are

Compass started as the open part of a larger product and kept the seams so
that product can run the same code with a different queue, config store and
model. Each seam is a module-level default plus an `install`/`set_*` hook:

| Seam | Open default | Hook |
|---|---|---|
| Job queue | `compass_core.queue.StoreQueue` (the jobs table, polled by the daemon) | `set_job_queue` |
| Map store | `infra_map.graph_store.open_store`: the SQLite file | `set_graph_store_factory` |
| Workspace config | the JSON file | `set_workspace_config_loader` / `saver` |
| Events | the job store | `set_event_sink` |
| AWS sessions | `compass_core.sessions`: the runtime identity for the control side; the two roles `connect` created for the target, every session named `compass-<captain>-<job>` and narrowed to the captain's declaration | `set_session_factory` |
| Model | Anthropic (`ANTHROPIC_API_KEY`) or none | `set_model_factory` |
| Email | SMTP endpoint you configure | `set_email_relay` |
| Inbox sources | the core's kinds plus each active captain's `inbox` function | `compass_core.inbox.register_source` (`compass_api.extensions.register_inbox_source`) |
| API: workspace, status sections, bootstrap hooks, auth paths and scopes, tabs | one workspace, open sections, `/api/` gated | `compass_api.workspace.install`, `compass_api.extensions` |
| Dashboard: tabs, settings sections, banners | the open tabs | `app/lib/extensions.ts` (`productTabs`, `productSettingsSections`, `productBanners`, `productProviders`) |

If you are adding a feature, you almost never need a new seam; add a command
and, if the dashboard needs it, a route.

## Updates

`compass_core.updates` reads `releases.json`, which the release workflow keeps
on the `releases` branch, at most every six hours. It compares the feed with
`compass_core.__version__`. When a release is out it also reads the release
notes, an RSS feed on beamreach.ai, and keeps those of the versions in between
(`release_notes`); a notes feed that cannot be read never fails the check. The
bootstrap, `/api/updates`, `/api/status` and `compass update check` all show
the result. Applying a release is not
something the Compass containers can do: they have no Docker socket. The
opt-in `updater` service has one. The two sides talk through files on the
`./extensions` volume. The API writes `update.json` and `status.json`, the
latter with the active job count. The updater writes `update.result.json` and
`rollback.json`. Those files work when the API is down. On the next start,
the entrypoint runs `compass migrate` (`compass_core.migrations`) and rebuilds
the extensions for the new core.

```
dashboard ─POST /api/updates/apply─► api ─► extensions/update.json
updater (docker/updater/updater.sh, every 30s) ─► pull, verify digests, .env, compose up, /health
                                               └► update.result.json, rollback.json
```

## Running jobs

```
dashboard ──POST /api/…──► api ──► queue.enqueue(captain, command, payload)
                                           │ writes {status: queued} to the jobs table
compass serve ──every 2s──► claims a queued job ──► dispatch(captain, command, payload)
                                           │ heartbeat every 30s; events as they happen
                                           └► {status: completed | failed}
```

A job with no heartbeat for three minutes is marked dead, so a crashed daemon
never leaves the dashboard waiting. The CLI runs commands inline, in the same
process, and still records them.

## Hosts: products built on Compass

A captain adds one capability. A *host* changes how the whole install behaves:
where workspaces come from, which queue runs jobs, which model answers, extra
API routers and startup work. A host is a function registered under the
`compass.hosts` entry point group (or named in `COMPASS_HOSTS` as
`module:function`). `compass_core.hosts.install()` runs every host once, at the
start of the CLI and before the API is assembled, so with a host installed
`compass api` and `compass serve` are that host's API and daemon.

A host works through the same seams as everything else:
`compass_core.queue.set_job_queue`, `compass_core.config.set_workspace_config_loader`,
`compass_core.llm.set_model_factory`, `compass_core.captains.register`, and in
the API `compass_api.extensions` (`register_router`, `register_startup`,
`register_shutdown`, the bootstrap and status sections). The open defaults step
aside for what a host set: the job queue, and the flags that hide closed tabs.
A host that fails to install is logged and the open defaults stay in effect.
