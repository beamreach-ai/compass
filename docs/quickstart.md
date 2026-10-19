# Compass — quickstart

Compass maps an AWS account to the Terraform and repositories that own it,
then acts on the map: plants decoy credentials that trip when used, imports
unmanaged resources into Terraform, and keeps the pull requests it opens alive.
It runs on your machine: a dashboard and API, a daemon, and a local DynamoDB.
Your credentials, your account.

You need Docker, an AWS profile in `~/.aws` with read access to the account,
and a GitHub token with `repo` scope (`gh auth token` prints yours).

## 1. Install

```sh
mkdir compass && cd compass
for f in docker-compose.yml nginx.conf .env.example; do
  curl -fsSO https://raw.githubusercontent.com/beamreach-ai/compass/main/$f
done
cp .env.example .env
$EDITOR .env      # AWS_PROFILE, AWS_REGION, GITHUB_TOKEN; ANTHROPIC_API_KEY for Claude
```

## 2. A workspace

```sh
docker compose run --rm compass init_config workspace=local region=us-east-1 \
    github_repo=<owner>/<infra-repo> terraform_root=<path to the root module>
docker compose run --rm compass init_tables
docker compose up -d
```

`init_config` prints a `login` block: the dashboard URL, user `admin` and a
generated password (kept in `config/local.json`). Open **http://localhost:8080**
and sign in with it. Lost it? `docker compose run --rm compass login`. To choose
your own, set `APP_USERNAME` and `APP_PASSWORD` in `.env` before `up`.
Start the first map scan from **Maps → Run mapping** (minutes; the **Queue** tab
shows it running). Everything the dashboard starts — scans, decoy plants,
imports — is a job the `compass` daemon runs; the dashboard never waits on it.

`init_config` writes `config/local.json` (your GitHub token goes in it; the
file is yours, not in any repository). The daemon also consumes decoy alerts,
syncs the pull requests Compass opened every five minutes and answers
reviewers, and re-scans the map on an interval if `COMPASS_MAP_INTERVAL` is set.

Every action in the dashboard is also a command:
`docker compose run --rm compass <captain> <command> key=value …` (JSON out).

## 3. Decoys

Decoys report to an SQS queue in the account. Create it once, enable a
response, then plant:

```sh
docker compose run --rm compass init_alerts         # creates compass-decoy-alerts-local; needs write access once
docker compose run --rm compass honeypot action_set action_type=iam_deactivate_key mode=autopilot risk_max=2
docker compose run --rm compass honeypot create honeytoken_class=aws_credential name=svc_nightly_backup region=us-east-1
```

`create` opens a pull request on your infra repo: a module with the decoy (an
IAM user whose key is never used legitimately) and an EventBridge rule that
reports any use to the queue. Merge and apply it. Then:

```sh
docker compose run --rm compass honeypot arm honeytoken_id=<id>     # verifies user, key and rule; marks it armed
docker compose run --rm compass honeypot fire honeytoken_id=<id> secret_access_key=<from the apply output>
docker compose run --rm compass honeypot incidents                   # what fired, newest first
```

The secret is in the module's outputs (`terraform output -raw`); leave the key
where an attacker would look. `fire` needs the daemon running and a CloudTrail
trail logging management events in the account. Classes: `aws_credential`,
`db_credential`, `ssh_key`, `s3_object`, `api_token`, `dns_record`.

`action_set`'s `mode` is `off` (recommend only), `approve` (wait for a human)
or `autopilot`: autopilot runs the action — deactivate the decoy key, restart
the task that leaked it — only when its deterministic risk is at or below
`risk_max`. In the config, `prs.watcher.mode` and `risk_max` work the same way
for pull-request revisions.

## 4. Code-as-cloud

```sh
docker compose run --rm compass iac import repo=<owner>/<infra-repo> module_path=<root> \
    resource_ids='["arn:aws:s3:::my-bucket"]'
```

Terraform (in the image) generates the configuration, Compass rewires literals
to references in your modules, verifies with a plan, and opens a pull request
with the import blocks. `verified: false` with a warning means the plan could
not run with your credentials, not that the import is wrong.

## 5. Day to day

```sh
docker compose logs -f compass api               # the daemon and the API
docker compose run --rm compass prs sync          # sync pull requests now, not on the tick
docker compose run --rm compass honeypot endpoint_add type=webhook name=ops webhook_url=https://…
docker compose run --rm compass mapper export format=json out=/compass/map/map.json
docker compose run --rm compass mapper export format=html out=/compass/map/index.html   # the map as one HTML page
docker compose down                               # stop; data stays in the volume (down -v wipes it)
```

## 6. Updates

Compass checks for a new release every six hours: one anonymous request for
`releases.json` on the repository's `releases` branch, with the running version
in the user agent and nothing else. `COMPASS_UPDATE_CHECK=off` in `.env` stops
it. When a release is out, a banner says so and **Settings → Compass** shows
what changed. Nothing changes version without you.

The card's **What's new** lists every release between the one you run and the
one on offer, newest first: what each brings and what to do after the update.
Compass reads these notes from the RSS feed at
`https://beamreach.ai/compass/releases/feed.xml`, with the same request rules as
the check. `COMPASS_RELEASE_NOTES_URL` points it at a mirror or a file; `off`
stops just the notes. When the notes cannot be read, the update itself is
offered as before.

Without the updater, the card shows the two commands. Run them next to your
`docker-compose.yml`:

```sh
sed -i.bak 's/^COMPASS_VERSION=.*/COMPASS_VERSION=0.5.0/' .env   # or edit the line
docker compose pull && docker compose up -d
```

With the updater, the card has an **Update** button:

```sh
docker compose --profile updater up -d
```

The updater is a small container: the Docker CLI and one shell script. It
mounts the Docker socket, and whoever controls the socket controls this
machine as root. Turn it on only where that is acceptable. It pulls the
release's images and refuses any whose digest differs from the release's. It
waits for running jobs and keeps the old `.env` as `.env.bak.<version>`. Then
it recreates the services and checks health. When they do not come up within
five minutes, it puts the previous version back. **Roll back** stays on the card
for seven days after an update.

From a shell:

```sh
docker compose run --rm compass update check                  # what runs, what is out, what the updater did last
docker compose run --rm compass update apply [version=0.5.0]   # needs the updater; prints the commands without it
docker compose run --rm compass update rollback
```

`apply` refuses a downgrade, a version that cannot be reached directly, and a
version an installed captain does not support. `force=true` overrides each.

Policy: `COMPASS_AUTO_UPDATE` in `.env`, or the select on the Compass card.

| Value | What happens |
|---|---|
| `notify` (default) | The banner and the card. Nothing applies without a click. |
| `patch` | The updater applies `x.y.Z` releases on its own when no job runs. |
| `minor` | The same for `x.Y.z` releases too. |
| `off` | No checks at all. |

`COMPASS_UPDATE_WINDOW=03:00-05:00` limits automatic updates to those hours in
the machine's `TZ`. An automatic update never skips a version it cannot reach
directly and never installs a prerelease. One that fails is rolled back and not
retried. `COMPASS_UPDATE_CHANNEL=prerelease` follows release candidates.

A new version runs `compass migrate` on start: missing tables, then the
migration steps it ships, once each. Captains are reinstalled against the new
core. A captain whose `requires_core` excludes it shows as incompatible.

## From source

`CONTRIBUTING.md` has the venv and the `docker-compose.build.yml` override
that builds the two images from a checkout.

## Where things are

| | |
|---|---|
| Config | `./config/local.json` (container: `/compass/config/local.json`) |
| Dashboard and API | http://localhost:8080 (`COMPASS_PORT`); the API under `/api/` |
| Map viewer files | `./map/` (also at `/map/` on the dashboard's port) |
| Updates | `./extensions/update*.json`, `status.json`, `rollback.json`: what the API and the updater exchange |
| Tables | `compass_{map,items,aws_inventory,tf_inventory,tf_state,progress}_<workspace>` in the `dynamodb-data` volume |
| Branches | `beamreach/honeypot/<name>`, `beamreach/iac-import/<hash>` |
| Resource tags | `beamreach:honeytoken-id`, `beamreach:managed` on everything Compass plants |
