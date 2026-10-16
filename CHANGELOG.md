# Changelog

One section per release, newest first: `## <version> — <date>`. The release
workflow refuses a tag without one, and reads from it what the dashboard shows
next to the Update button: the first paragraph is the summary, the bullets under
`### After the update` are the notes an operator needs after applying it.

## 0.5.2 — unreleased

An end-to-end suite that runs on every pull request, and the bugs it found on
its first runs.

- `pytest e2e` (docs/e2e.md): the stack built from the commit, the shipped
  compose file and the quickstart's commands, against a fake AWS account (moto)
  and a fake GitHub, driven through a real browser, the API and the `compass`
  command. 74 tests over install, sign-in and tokens, the dashboard, settings,
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
- `compass status` in the CLI says when a release is out and when the compose
  file asks for another version than runs; the status skill mentions it.
  The API version stays 2026.10.1: the additions do not need a CLI that
  knows them, and a bump would have made every CLI call 0.5 incompatible.

## 0.5.1 — unreleased

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

## 0.5.0 — unreleased

Updates: the dashboard says when a release is out and applies it with one
click when the updater service runs; `compass update check | apply | rollback`
from the shell; `compass migrate` on every new version.

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

The Compass CLI and MCP server (`cli/`, `@beamreach/compass`) move into this
repository, with a plugin interface; hosts built on Compass install their
seams before the open defaults.

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
