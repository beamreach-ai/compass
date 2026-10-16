# Contributing

Thanks for looking. Compass is small enough that one person can hold the
whole thing in their head; please keep it that way.

## Before you start

- **Bugs and small fixes**: open a pull request. A failing test that shows the
  bug is the best first commit.
- **Anything that changes behaviour** (a new command, a new decoy class, a new
  tab): open an issue first and say what problem it solves. Compass does a few
  things on purpose; a feature that is useful but off-centre is better as a
  separate tool that uses the API.
- **Security**: see [SECURITY.md](SECURITY.md); do not open a public issue.

## Developer Certificate of Origin

Every commit is signed off (`git commit -s`), which adds
`Signed-off-by: Your Name <you@example.com>` and means you agree to the
[DCO](https://developercertificate.org/): you wrote the change or have the
right to submit it under the Apache-2.0 licence. No CLA.

## Setting up

```sh
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,anthropic]" ruff
pytest -q                      # moto fakes AWS; no credentials needed
ruff check .

cd dashboard && npm ci && npx tsc --noEmit && NEXT_PUBLIC_EDITION=open npm run dev
```

To run the whole stack from this checkout rather than the published images:

```sh
pip install build && python -m build --wheel
docker compose -f docker-compose.yml -f docker-compose.build.yml build
docker compose run --rm compass init_config workspace=local region=us-east-1 github_repo=<owner>/<repo> terraform_root=<root>
docker compose run --rm compass init_tables && docker compose up -d
```

[docs/architecture.md](docs/architecture.md) says what lives where and why.

## Code style

The rule behind all of these: the reader is a stranger who has the file open
for the first time and will not open a second one.

### Python

- Python 3.12, type hints on every signature, `from __future__ import
  annotations` at the top. `ruff` is the linter (`pyproject.toml` has the
  config); line length 140.
- One module does one thing and its docstring says what, in one or two
  sentences, in plain words. Say what the module is *for*, not what it
  contains.
- Comments explain what the code cannot: why, not what. A comment that
  restates the next line is deleted. Spell out the surprising part
  (`# the file is read fresh every time anyway`).
- Names are words: `honeytoken_id`, not `htid`; `resolve_target`, not
  `rt`. Captain, command, workspace, target, decoy, honeytoken, incident,
  placement are the vocabulary — see `docs/architecture.md` — don't coin
  synonyms.
- No globals with hidden state except the registries in
  `compass_core.dispatch`, `compass_core.queue`, `compass_core.config`,
  `compass_api.extensions` and `compass_api.workspace`, which exist so that
  a larger product can plug in behind the same API. If you need another
  one, say why in the pull request.
- Errors a user can act on are one line and say what to do
  (`workspace_id is required: set COMPASS_CONFIG or pass workspace_id=…`).
  Everything else raises; the CLI turns `ValueError`/`LookupError` into a
  one-line message and exit code 2.
- AWS: go through `compass_core.aws.build_target_session`; never build a
  boto3 client from environment variables in a captain. Every list call is
  paginated. Every write Compass makes to an account carries the
  `beamreach:managed` tag (the name is the tag namespace, not a brand
  requirement; changing it is a migration).
- GitHub: through `compass_core.github` / `compass_core.github_prs`. Branch
  names are `beamreach/<captain>/<slug>`.
- Terraform Compass writes is Terraform a person would write: `terraform fmt`
  clean, one module per decoy, variables with descriptions, no provider
  blocks inside modules.
- Logging: `logging.getLogger("<captain>.<module>")`, messages are
  sentences without trailing punctuation, no f-strings in log calls (use
  `%s`).

### Tests

- `pytest`, under `tests/<package>/`, file per module. A test name is the
  sentence it proves: `test_init_config_writes_a_complete_workspace_file`.
- AWS is `moto` (`@mock_aws`); GitHub is a fake class with the two methods
  the code calls; the model is a `TextModel` stub. No test reaches the
  network and none needs credentials.
- Prefer one test that walks a realistic path over five that each check a
  branch. Assert on behaviour the user would notice, not on call counts.
- A bug fix comes with the test that would have caught it.

### TypeScript (dashboard)

- Next.js 14 app router, strict TypeScript, Tailwind. `npx tsc --noEmit`
  clean. Components in `app/components/`, one tab per file; helpers that
  format or derive view state in a sibling `*Views.ts` / `*Format.ts`.
- The edition switch is `app/lib/edition.ts`: anything branded is behind
  `isOpenEdition`, never behind a string compare elsewhere. A new tab or
  settings section goes through `app/lib/extensions.ts` and `useShell()`
  (`app/lib/shell.tsx`), the same way a larger deployment adds its own.
- Copy in the UI is plain English, sentence case, no exclamation marks, no
  product vocabulary the sidebar does not already use.

### Releases

A release is a tag `v<version>` matching `pyproject.toml`, and a
`## <version>` section in `CHANGELOG.md`. The workflow refuses a tag without
the section. Its first paragraph is the summary the dashboard shows. Bullets
under `### After the update` are the notes beside the Update button. The
workflow publishes the three images, the GitHub Release and `releases.json`. It
then bumps the `COMPASS_VERSION` defaults on `main`.

### Commits and pull requests

- Subject: `<area>: what changed`, lower case, under 72 characters —
  `mapper: record the scan so the dashboard enables its tabs`. The body says
  why and what a reader of `git log` needs to know; it is prose, not a list
  of files.
- One change per pull request. Refactors travel alone.
- CI must be green: `pytest`, `ruff`, `tsc`, the three image builds.
- Nothing from your account in the diff: no account ids, no ARNs with real
  ids, no tokens, no screenshots with real names. Test fixtures use
  `123456789012` and `example.com`.
