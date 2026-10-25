---
name: compass-open
description: Install and run Compass (infra map, decoys, code-as-cloud, PR watcher, dashboard) on this machine with Docker Compose — no source build, no Python: docker-compose.yml, nginx.conf and an .env; the dashboard on http://localhost:8080. Use when asked to set up, deploy, try out, or run Compass locally, or to map an AWS account / plant a decoy / import resources with it.
---

# Deploying Compass locally

Follow `docs/quickstart.md` step by step; do not improvise commands. The
stack is five containers (six with the opt-in `updater`): `dynamodb`, `compass` (the daemon), `api`,
`dashboard`, `nginx` on port 8080. Commands are `docker compose run --rm
compass <captain> <command> key=value …` and print JSON; an unknown command, a
missing config or a bad argument is one line on stderr with exit 2.

Before step 1, confirm with the user: which AWS profile in `~/.aws` (`connect`
uses it once to create two IAM roles, a queue and a runtime user; nothing else
ever sees it), which GitHub repository pull requests should go to, and whether
they want decoys applied (an IAM user and an EventBridge rule in their account).

Order, and what to check after each step:

1. An empty directory (not this checkout) with `docker-compose.yml`,
   `nginx.conf` and `.env` (from `.env.example`) copied from the repository
   root: `AWS_REGION`, `GITHUB_TOKEN` (use `gh auth token` if logged in),
   optionally `ANTHROPIC_API_KEY`. No `AWS_PROFILE`: the services refuse to
   start with one.
2. `init_config …` — a `path`, `github_token: from …` (not `missing`), and a
   `login` block (URL, user, generated password). Give the user the login.
   If it says `warning`, sign-in is off: set `APP_PASSWORD` in `.env`.
3. `init_tables` — seven tables `created` (or `present` on a re-run).
3b. `docker compose run --rm -v ~/.aws:/root/.aws:ro -e AWS_PROFILE=<profile> compass connect account=<id> region=<region>`
   — `steps` ending in `done`, `aws.roles.read` and `.act`, `aws.control.runtime_arn`,
   and `verification.assumed.read.ok` true. A denied action in
   `verification.denied` means the read policy did not apply; say so. It is the
   only command that writes to the account on its own (two roles, a queue, a
   user). A second run prints the same with nothing `created`.
4. `docker compose up -d` — `docker compose ps` shows all five running;
   `curl -s localhost:8080/health` is `{"status":"ok"}`. Give the user
   http://localhost:8080 and the sign-in (`docker compose run --rm compass login`
   prints it).
5. The first map: **Maps → Run mapping** in the dashboard, or
   `mapper run`. It takes minutes; the **Queue** tab shows the job. The map
   is a SQLite file on the `map-data` volume; `mapper backup` copies it while
   the stack runs. On an install from before 0.6, the first start after the
   update copies the DynamoDB map into it once (`compass migrate` in the
   logs: `0.6.0-map-sqlite`); the table stays for a rollback
   (`COMPASS_MAP_STORE=dynamodb` in `.env`).
6. Decoys: `honeypot action_set` (the queue exists since `connect`), then plant from the dashboard's Honeypots tab or `honeypot create …` — a
   pull request URL. The user merges and applies; then `honeypot arm` (with
   `secret_access_key=…` from the apply output it keeps the secret, so `fire`
   needs none) and `honeypot fire`; incidents show in the dashboard and in
   `honeypot incidents`.
7. Code-as-cloud: the IaC tab, or `iac import …` — a pull request URL;
   `verified: false` with a warning means the plan could not run with the
   given credentials.

8. Patches: after the first map, `patch feed_run` (the feeds, a few minutes
   the first time) then `patch tick` — `summary.open` findings and, for the
   exploited, exposed or severe ones, `prs.opened` pull request URLs on the
   fake or real GitHub. `patch findings` lists them with the fixed versions;
   the **Patches** tab shows the same. `patch feed_inject` is refused unless
   `COMPASS_PATCH_INJECT=1` is set; it is for the test suite, not for an
   install.

9. Updates (only when asked): `update check` shows the running version, the
   compose file's `COMPASS_VERSION`, and the latest release, with the notes of
   every release in between under `update.release_notes`. Without the
   updater, apply with the two commands it prints. Do not start the updater
   (`--profile updater`) unless the user agrees: it mounts the Docker socket,
   which is root on their machine. `update apply` then answers `requested`.
   Check `update check` until `result.status` is `updated`, or `failed` with
   the previous version still running.

Credentials never go into a repository: `config/local.json` holds the GitHub
token, `.env` the dashboard password. `ANTHROPIC_API_KEY` in `.env` enables
Claude; without it Compass runs model-free and says so.

Do not run `honeypot respond` by hand on an account the user did not name for
it: it acts on the account (deactivates keys, restarts tasks).
