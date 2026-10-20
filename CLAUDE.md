# Compass — notes for AI coding agents

Read `README.md` for what Compass does and `docs/architecture.md` for what
lives where. `CONTRIBUTING.md` is the style guide; follow it, including the
commit format and the DCO sign-off.

## Working here

- Set up once: `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev,anthropic]" ruff`.
  Run `.venv/bin/pytest -q` and `.venv/bin/ruff check .` before saying a
  change is done; for the dashboard, `cd dashboard && npx tsc --noEmit`.
  Report the exact command and whether it passed.
- Tests never need AWS credentials or the network (`moto`, fakes). If a test
  you wrote does, it is wrong.
- A change to a command's payload or output is a change to the API, the CLI,
  the skill under `.claude/skills/compass-open/` and the docs — update them
  in the same pull request. `tests/compass_core/test_core_cli.py` pins the
  command tables.
- Do not add a dependency without saying why in the pull request; the
  runtime image is `python:3.12-slim` plus Terraform and the list in
  `pyproject.toml`.
- Do not run `honeypot respond`, `honeypot fire`, `connect` or anything
  that writes to an AWS account unless the user named the account and asked.
  `mapper run` and `iac import` read; everything else that touches AWS says
  so in its docstring.

## Things that look like bugs and are not

- `compass_core.dispatch`, `queue`, `config`, `compass_api.extensions` and
  `compass_api.workspace` are registries with a default that a larger
  deployment replaces at import time. Keep the open default simple; do not
  remove the hook.
- `dashboard/app/lib/extensions.ts` exports empty registries and
  `app/lib/shell.tsx` exposes more than the open tabs use: that is the seam a
  larger deployment plugs its tabs into. Keep both; do not inline them.
- `beamreach:` tag and event names, `beamreach/` branch prefixes: the
  namespace Compass has always used. Renaming is a migration for every
  existing install, not a cleanup.
- The jobs table is a plain DynamoDB table and the daemon polls it every two
  seconds. That is the whole queue; there is no broker.

## When the user asks you to install or try Compass

Use the `compass-open` skill (`.claude/skills/compass-open/SKILL.md`): it is
the quickstart in order, with what to check after each step. Do not improvise
the steps from memory.
