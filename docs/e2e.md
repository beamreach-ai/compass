# End-to-end tests

`pytest e2e` starts the real Compass, the images built from your checkout and the
shipped `docker-compose.yml`, and uses it the way a person does: through the
dashboard in a real browser, through the API, and through the `compass` command.
It runs against a fake AWS account and a fake GitHub, so it needs no account, no
token and no model. CI runs the part a pull request needs on every pull request,
everything every night and before every release ("In CI" below).

The unit tests (`pytest`) check functions in isolation in seconds. This suite
checks that the pieces work together as shipped: the compose file, the images, the
quickstart's commands, the dashboard against the API, the daemon against the jobs
table, GitHub and AWS calls with real clients. It is slower, a run takes 25–45
minutes, and it is the one that catches what only shows up when it all runs.

## Running it

You need Docker with compose, Python 3.12 and network access to pull images and
the Terraform AWS provider.

```sh
.venv/bin/pip install -e ".[dev,e2e]"
.venv/bin/python -m playwright install chromium
PATH="$PWD/.venv/bin:$PATH" pytest e2e
```

The first run builds the three images (about ten minutes). Then:

| To | Run |
|---|---|
| Reuse the last build | `E2E_BUILD=0 pytest e2e` |
| Run one area | `pytest e2e/test_06_decoys.py` |
| Run one test by its ID | `pytest e2e -k dec_5` (IDs are in §4, lower case, `-` as `_`) |
| Watch the browser | `E2E_HEADED=1 pytest e2e -k ui_` |
| Keep the stack afterwards | `E2E_KEEP=1 pytest e2e`, then the dashboard is on http://127.0.0.1:18480 |
| Rerun tests on the kept stack, without a rebuild | `E2E_REUSE=1 pytest e2e -k cap_4` (the stack is as the last run left it, so a test that creates something by name may meet what an earlier run made; for a clean answer, run without it) |
| Skip the slow update test | `E2E_SKIP_COMPOSE_UPDATE=1 pytest e2e` |
| Use other ports | `E2E_PORT`, `E2E_MOTO_PORT`, `E2E_FAKES_PORT` (18480, 15000, 19000) |

Tests run in file order (`test_00_install.py` first) on one shared stack. Later
areas build on earlier ones: the map scanned once (the `mapped` fixture), decoys
planted before incidents are opened. Running a single file or `-k` works, because
each test asks for what it needs, but a test that depends on an earlier one in its
file says so in its body.

## In CI

`.github/workflows/e2e.yml` is the one definition, run three ways:

| When | What runs |
|---|---|
| Every pull request (`ci.yml`) | The tests the change needs, decided by `.github/scripts/e2e_plan.py` |
| Nightly on `main`, or by hand (Actions → e2e → Run workflow) | Everything |
| A release tag (`release.yml`) | Everything, before any image is published |

The images are built once into GitHub's build cache, then four shards run in
parallel, each with its own stack:

| Shard | Files |
|---|---|
| A | install, sign-in, shell, settings, demo mode (`test_00`–`test_03`, `test_13`) |
| B | captains, the map (`test_04`, `test_05`) |
| C | decoys, code-as-cloud, pull requests (`test_06`–`test_08`) |
| D | jobs, updates, release (`test_09`–`test_11`) |

What a pull request runs:

- **Nothing**, when only what does not run in the stack changed: docs, the
  changelog, the CLI, unit tests. The check still reports, and passes.
- **One captain's tests plus the install and shell smoke tests**, when only that
  captain's folder changed (`captains/honeypot`, `iac`, `prs`, `librarian`, the
  example captain).
- **Those files**, when only end-to-end test files changed, with the files they
  build on.
- **Everything** for any other change: `compass_core`, `compass_api`, the
  dashboard, `docker/`, the compose file, the harness and fakes, and the mapper,
  which every other captain reads. A rule that is not sure runs everything.
- **The real compose update** (UPD-2, about eight minutes) only when what an
  update touches changed: the images, the compose file, the updater, the
  entrypoint, the update and extension code.

The nightly run catches what the shortcuts let through within a day. The check a
branch rule requires is **`e2e / e2e`**. It passes when every planned shard
passed, or when the plan ran nothing.

To see what a change would run:

```sh
git diff --name-only origin/main...HEAD > /tmp/changed.txt
python .github/scripts/e2e_plan.py --changed /tmp/changed.txt
```

## Reading a failure

A failed test leaves a directory under `e2e/.artifacts/<test id>/`:

| File | What |
|---|---|
| `trace.zip` | The browser's recording: every action, network call, console message and a DOM snapshot at each step. Open it with `playwright show-trace e2e/.artifacts/<test>/trace.zip`. |
| `screenshot.png` | The page when the test failed |
| `console.txt` | Errors the page logged |
| `stack-logs.txt` | The last 300 lines of every service's log |

In CI each shard uploads it as `e2e-artifacts-<shard>`, and the job log ends with the
stack's errors.
`E2E_KEEP=1` leaves the stack up after a local run; `cd e2e/.work/compass-e2e` and
use `docker compose -f docker-compose.yml -f compose.e2e.yml logs` as on any install.

## How it works

```
 e2e/conftest.py ─┬─ builds compass-e2e/{compass,dashboard,updater}:local from this checkout
                  ├─ makes e2e/.work/compass-e2e: docker-compose.yml, nginx.conf, .env from .env.example
                  ├─ adds e2e/compose.e2e.yml:   moto (fake AWS)   fakes (fake GitHub, webhooks, release feed, catalog)
                  ├─ seeds both from e2e/fixture.py
                  └─ runs the quickstart: init_config, init_tables, connect (moto's keys as the profile), up -d

 Compass ── AWS_ENDPOINT_URL ──────► moto          (DynamoDB stays on the stack's own dynamodb-local)
         ── GITHUB_API_URL, GITHUB_GIT_URL ► fakes /github, /git
         ── COMPASS_UPDATE_URL ────► fakes /feed     COMPASS_CATALOG_URL ► fakes /catalog.json
         ── COMPASS_RELEASE_NOTES_URL ► fakes /feed/notes.xml
```

**The world** (`e2e/fixture.py`) is one AWS account and one GitHub organisation.
The repository `acme/infra` holds Terraform whose S3 backend points at a state file
in the account. Five resources are in that state (managed), one of them with a value
that differs from the account (drifted: the queue `acme-jobs`), two are in no state
(unmanaged), and a secret and a role give decoys something to sit next to. Expected
counts in the tests come from this file.

**The fakes** (`e2e/fakes/app.py`) are one small server. Its GitHub answers the REST
calls PyGithub and Compass make, the one GraphQL query the PR watcher sends, and git
clone and push over HTTP (`git http-backend` over a bare copy of the in-memory
repositories). A control API under `/_control/` lets tests do what a person does on
GitHub (merge, close, review) and see what Compass did (the pull requests it opened
and their files, the webhooks it called, the feed requests it made).

**What the suite does that a person would.** Terraform cannot apply against moto, so
when a test "merges and applies" a decoy's pull request, it reads the module Compass
wrote and creates in moto what the module declares (`apply_aws_credential`); that also
checks that the generator and the arming check agree on names. An attacker using a
decoy key is a message on the alert queue in the shape the module's EventBridge rule
sends (`send_alert`).

**What it does not cover**, by design: anything that needs Claude (PR revisions,
incident summaries, drift-fix HCL; the model-free behaviour is tested instead), and
anything only a real AWS account shows (the mapper against real APIs, a decoy key
firing through CloudTrail, Terraform plans that verify). Those are the manual checks
before a release.

## Adding a test

1. Find the area's file (`test_06_decoys.py`) and the next free ID in §4; add the row
   to §4 in the same pull request.
2. Name the test `test_<id>_<the sentence it proves>`, for example
   `test_dec_13_a_decoy_in_a_second_region_is_armed_there`.
3. Use the fixtures: `stack` (the install: `stack.api` is a signed-in HTTP client,
   `stack.cli(...)` runs a command and returns its JSON, `stack.aws_client("iam")`
   talks to moto, `stack.fakes` to the fake GitHub, `stack.repo()` shows the pull
   requests, `stack.env(...)` and `stack.restart(...)` change settings), `page` (a
   signed-in browser page; `page.open(path)`), `anonymous_page`, `phone_page`, and
   `mapped` (the map, scanned once).
4. Assert on what a person would notice: the pull request's files, the incident, the
   text on the page. Wait for things with a deadline, never a bare `sleep`.
5. A test that changes a setting puts it back in a `finally`, so the next test sees
   the stack as the fixture left it.
6. If Compass needs to call something the fakes do not answer, add the endpoint to
   `e2e/fakes/app.py` with the shape the real service returns.

## 4. What is tested

IDs are stable: a failure in CI points here. "Via" says how the test drives Compass.

### 4.1 Install and first run (`test_00_install.py`)

| ID | Test | Via |
|---|---|---|
| INS-1 | The quickstart from an empty directory: five services running, `/health` ok | CLI |
| INS-2 | `init_config` prints a login that signs in; a wrong password is refused | CLI, API |
| INS-3 | `init_config` refuses to overwrite a workspace, in one line, exit 2 | CLI |
| INS-4 | `init_tables` creates seven tables and says `present` the second time | CLI |
| INS-5 | `APP_USERNAME`/`APP_PASSWORD` in `.env` win over the generated sign-in | CLI, API |
| INS-6 | Without any password, Compass warns that anyone can use it | CLI |
| INS-7 | An unknown command, an unknown captain and a missing config are one line, exit 2 | CLI |
| INS-8 | `compass migrate` runs on start and runs no step twice | CLI |

### 4.2 Sign-in, sessions and API tokens (`test_01_auth.py`)

| ID | Test | Via |
|---|---|---|
| AUT-1 | Every `/api/` route needs a session or a token; `/health` does not | API |
| AUT-2 | Sign in and out in the browser; signed out, the dashboard goes to the login page | UI |
| AUT-3 | Read, operate and admin tokens can each do what their scope allows and are refused the rest | API |
| AUT-4 | A revoked token is refused at once | API |
| AUT-5 | `READ_ONLY=1`: writes are refused and the page says so | API, UI |

### 4.3 The dashboard shell (`test_02_shell.py`)

| ID | Test | Via |
|---|---|---|
| UI-1 | Every tab opens without a console error; the closed-edition tabs are absent | UI |
| UI-2 | The sidebar: Settings at the bottom above the account, Decoys and Settings list their sections while open | UI |
| UI-3 | Each Settings section link scrolls to its heading | UI |
| UI-4 | Every blue button on every tab has white text | UI |
| UI-5 | On a phone the menu is a drawer that closes on Escape and on navigation | UI |
| UI-6 | The sidebar shows the target account and the workspace | UI |

### 4.4 Settings: integrations (`test_03_settings.py`)

| ID | Test | Via |
|---|---|---|
| SET-1 | AWS local IAM saves and shows the account; an invalid entry is refused and saves nothing | API, UI |
| SET-2 | GitHub shows the token's login; a token GitHub rejects changes nothing; a new one replaces it | API, UI |
| SET-3 | The setup warning shows while GitHub is missing and goes once it is set | UI |
| SET-4 | Slack says it is not configured | UI |
| SET-5 | The GitHub token and the dashboard password never come back from the API | API |

### 4.5 Captains and extensions (`test_04_captains.py`)

| ID | Test | Via |
|---|---|---|
| CAP-1 | The Installed list shows the five shipped captains, each with its switch on | UI, API |
| CAP-2 | A captain switched off loses its tab and its work is refused; switched on, both come back | UI, API |
| CAP-3 | Switching off a captain others need asks and switches them off too; switching one on asks for its dependency | UI |
| CAP-4 | A row click opens the configuration; an invalid value is refused; a secret comes back masked and survives being saved back | UI, API |
| CAP-5 | Available lists the catalog; search filters it | UI, API |
| CAP-6 | Install the example captain from the page, restart from the page: it is installed, its tab is there, its command runs | UI |
| CAP-7 | Uninstall it from the page: after the restart it is gone, its configuration kept | UI, CLI |
| CAP-8 | A failed install keeps the previous set and the page says why | UI, API |
| CAP-9 | `COMPASS_CATALOG_INSTALL=off` and `COMPASS_CATALOG_ALLOW` limit what the page installs | API |
| CAP-10 | `captains`, `enable`, `disable`, `configure`, `catalog`, `install`, `uninstall` agree with the page | CLI |
| CAP-11 | `compass validate` passes the example and fails a broken copy with the reason | CLI |

### 4.6 The map (`test_05_map.py`)

| ID | Test | Via |
|---|---|---|
| MAP-1 | A scan started from the page shows its progress in the Maps panel and on other tabs; its events carry the progress data | UI, API |
| MAP-2 | The map has the seeded resources; managed ones are covered, unmanaged ones not, the queue is drifted on its visibility timeout; the repository, root module and state file are on it | API |
| MAP-3 | Search, focus, the resource-type filter and the three overlays work | UI |
| MAP-4 | A resource deleted from the account leaves the map on the next scan | API |
| MAP-5 | The map exports as JSON, DOT and HTML, and `/map/` serves the viewer | CLI, UI |
| MAP-7 | A scan that cannot reach AWS fails with the reason, and a retry works | API, UI |

MAP-6 (a scan of a real account) is a manual check before a release.

### 4.7 Decoys (`test_06_decoys.py`)

| ID | Test | Via |
|---|---|---|
| DEC-1 | `connect` created the queue in the control account with its policy and SQS-managed encryption; the daemon consumes it; `replant_alerts` has nothing to do on a fresh install | CLI |
| DEC-2 | Suggestions appear next to the seeded resources | API, UI |
| DEC-3 | Planting each of the six classes opens a pull request with one module, `terraform fmt` clean; planting from the page works | API, UI |
| DEC-4 | Arming before the apply says what is missing; after it, the decoy is armed | API |
| DEC-5 | An alert opens an incident with its blast radius and notifies the webhook endpoint | API, UI |
| DEC-7 | Responses in `off` only recommend, in `approve` wait for the approval and then deactivate the key, in `autopilot` deactivate it on their own | API |
| DEC-8 | An incident's report downloads, its alert is resolved, the incident is closed | API, UI |
| DEC-9 | Decommissioning opens a removal pull request and retires the decoy | API |
| DEC-10 | Autopilot plants every suggestion | API |
| DEC-11 | A webhook endpoint is added, tested (the call arrives) and deleted | API |
| DEC-12 | A decoy's secret is shown on request and never in a list or its detail | API |

DEC-6 (a real key firing through CloudTrail) is a manual check before a release.

### 4.8 Code-as-cloud (`test_07_iac.py`)

| ID | Test | Via |
|---|---|---|
| IAC-1 | The IaC tab lists the unmanaged resources, the drifted one and the coverage | API, UI |
| IAC-2 | Importing a resource opens a pull request with its import block (Terraform runs; the plan is "needs review" against moto) | API |
| IAC-3 | Without a model, fix-drift fails with the reason and opens nothing (the fix itself is written by the model) | API |
| IAC-4 | The pull requests Compass opened are listed | API, UI |

### 4.9 Pull requests: the watcher (`test_08_prs.py`)

| ID | Test | Via |
|---|---|---|
| PRS-1 | Sync follows the pull requests Compass opened to merged and closed | API |
| PRS-2 | A reviewer's request is parked for a person and listed under Needs you | API, UI |
| PRS-3 | A pull request can be paused and resumed | API |
| PRS-4 | The watcher policy saves, refuses an unknown mode, and the next review is handled under it | API |

PRS-5 (a revision commit written by Claude) is out of this suite: it needs a model.

### 4.10 Jobs and the queue (`test_09_jobs.py`)

| ID | Test | Via |
|---|---|---|
| JOB-1 | The newest jobs are listed first even among hundreds | API, UI |
| JOB-2 | A failed job shows its error and can be restarted | UI, API |
| JOB-3 | A job whose daemon died is marked dead; new jobs run after a restart | CLI, API |
| JOB-4 | `/api/status` reports every section and degrades one at a time | API |

### 4.11 Updates (`test_10_updates.py`)

| ID | Test | Via |
|---|---|---|
| UPD-1 | A newer release shows a banner that can be dismissed until the next one; the card shows its notes | UI, API |
| UPD-2 | Without the updater the card shows the commands; with it, a release is applied, a wrong digest refused and a broken release rolled back (`tests/e2e/update_compose.sh`) | UI, CLI |
| UPD-3 | Roll back from the card asks the updater for the previous version | UI |
| UPD-4 | The updater waits while jobs run | CLI |
| UPD-5 | The policy saves from the card; `patch` asks for a patch release on its own and not for a minor one | UI, API |
| UPD-6 | With the check off no request leaves the stack, release notes included; on again, one anonymous request with the version | API |
| UPD-7 | The page says when the compose file asks for another version than runs | UI |
| UPD-8 | The banner's What's new opens the release notes of every version between the running one and the offered one, newest first, as plain text | UI, API |

### 4.12 Release artefacts (`test_11_release.py`)

| ID | Test | Via |
|---|---|---|
| REL-1 | The three images start; the updater image has docker compose, jq and curl | CLI |
| REL-2 | `CHANGELOG.md` has this release, and the release script reads it | CLI |
| REL-3 | The quickstart and the README give the commands this suite ran | — |

75 tests. The multi-architecture image builds are checked by the release workflow,
not here.

### 4.13 Access: grants by pull request (`test_12_access.py`)

| ID | Test | Via |
|---|---|---|
| ACC-1 | `connect` left the two roles; the act role starts empty; nothing is granted | API, moto |
| ACC-2 | Allowing a response proposes a grant (`pr_open`) and the access job opens the pull request with the module: both roles, the grant's statements | API, fake GitHub |
| ACC-3 | Merging it in the fake and "applying" (the test writes the act policy into moto) then verifying arms the grant; the status line counts it | API, moto |
| ACC-4 | Removing disarms at once and the next access job opens the revoke pull request, the module without the grant; the grant stays `revoke_pr_open` while IAM holds it | API, fake GitHub |
| ACC-5 | A statement added in the console is an `unexplained` finding of the self-audit; removing it clears the finding | API, moto |

The suite cannot run Terraform against moto: "the operator applies" is the test writing the
policy the module renders into moto, as the apply would.

### 4.14 Demo mode (`test_13_demo.py`)

| ID | Test | Via |
|---|---|---|
| DEMO-1 | With `APP_MODE=demo`, `compass demo seed` puts the synthetic world on the map beside the scanned account, a rescan leaves it alone, the same seed again changes nothing; outside demo mode the command refuses | CLI, API |

The stack runs these with `APP_MODE=demo` in `.env` and puts it back afterwards; the
read-only test (AUT-5) keeps `READ_ONLY=1` as its own thing.
