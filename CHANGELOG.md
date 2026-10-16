# Changelog

One section per release, newest first: `## <version> — <date>`. The release
workflow refuses a tag without one, and reads from it what the dashboard shows
next to the Update button: the first paragraph is the summary, the bullets under
`### After the update` are the notes an operator needs after applying it.

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
