# Captain development guide

This guide takes you from an empty directory to a captain that anyone running
Compass can install from Settings → Captains → Available. It assumes you know
Python and have used Compass once. The manifest's every field is in
[writing_a_captain.md](writing_a_captain.md); this guide is the path through it.

A captain is a Python distribution that Compass discovers through the
`compass.captains` entry point. It can contribute commands (work that runs as
jobs), API routes, a dashboard tab, background services, and a settings form.
Compass handles the rest: turning it on and off per workspace, refusing its
work when it is off, licences, the job queue, the Captains page.

## 1. Start from the example

```
cp -R examples/captain-hello ~/src/compass-captain-weather
cd ~/src/compass-captain-weather
```

Rename three things, and keep them equal:

| Where | From | To |
|---|---|---|
| the package directory | `captains/hello/` | `captains/weather/` |
| `name` in `captains/weather/manifest.py`, and the entry point key | `hello` | `weather` |
| `name` in `pyproject.toml` | `compass-captain-hello` | `compass-captain-weather` |

Update the module paths in the manifest (`captains.weather.commands`,
`captains.weather.routes:router`) and the entry point:

```toml
[project.entry-points."compass.captains"]
weather = "captains.weather.manifest:MANIFEST"
```

Two rules that are easy to break:

- **No `captains/__init__.py`.** `captains` is a namespace package shared with
  Compass and every other captain. An `__init__.py` there hides all of them.
- **Do not depend on `compass-open`.** Compass is already installed where your
  captain runs; a dependency on it would reinstall Compass inside the image.
  List only what your own code imports.

Run the checks now and after every change:

```
compass validate path=.
```

It runs what CI runs on the shipped captains: the manifest's rules,
compatibility with your Compass, the commands module registering, every route
under a declared prefix, the schema, name and tab clashes with installed
captains, and `pyproject.toml` agreeing with the manifest. It exits non-zero
and names each failure.

## 2. The local loop

You need a Compass to run against. The quickest is the open edition from a
checkout of this repository, in a virtual environment:

```
git clone https://github.com/beamreach-ai/compass && cd compass
python -m venv .venv && . .venv/bin/activate
pip install -e '.[anthropic,dev]'
pip install -e ~/src/compass-captain-weather     # your captain, editable
compass init_config workspace=local region=us-east-1
compass captains                                  # weather is listed, on
```

`pip install -e` means your edits take effect on the next start, with no
reinstall. The loop:

| To try | Run |
|---|---|
| A command | `compass weather forecast city=Lisbon --workspace local` |
| Its services once | `compass serve once=true` |
| Its routes and tab | `compass api` and the dashboard on `localhost:8080` (the compose file in this repository) |
| Turning it off | `compass disable captain=weather`, then the command again: it is refused |

Without installing it, `COMPASS_CAPTAINS=captains.weather.manifest:MANIFEST`
with your project on `PYTHONPATH` does the same for a quick experiment.

## 3. Commands

Commands are where the work goes. Each is a function from a payload dict to a
JSON-able dict, registered by the module the manifest names:

```python
from compass_core import dispatch
from compass_core.workspace import resolve

def forecast(payload):
    """Fetch the forecast for city= and record it."""
    workspace = resolve(payload)            # the workspace id comes in the payload
    section = workspace.config.get("weather") or {}
    ...
    return {"city": payload["city"], "summary": "..."}

COMMANDS = {"forecast": forecast}
dispatch.register("weather", COMMANDS)
```

The first line of the docstring is what the generic page and the CLI show.
The same function runs from the shell, from a job the dashboard queued, and as
a follow-up another command enqueues:

```python
from compass_core.queue import job_queue
job_queue().enqueue(captain="weather", command="forecast", payload={"workspace_id": ws, "city": "Porto"})
```

Things Compass already provides, so you do not build them (sending to the
workspace's Slack and webhook endpoints is not a core API yet; it lives inside
the decoy and PR captains):

| Need | Use |
|---|---|
| AWS credentials for the workspace's account | `compass_core.aws.build_target_session(config)` |
| GitHub, as the workspace's app or token | `compass_core.github` |
| A pull request with your change | `compass_core.github_prs.open_or_update_pr` |
| A model | `compass_core.llm.model_for(config)` (None when none is configured: degrade, do not fail) |
| Progress on the job's timeline | `compass_core.events.emit_job_event` |
| DynamoDB tables and keys | `compass_core.tables`, `compass_core.keys` |

## 4. Settings

Declare your section of the workspace config as JSON Schema in the manifest,
and Settings → Captains draws the form, validates it, and masks secrets:

```python
config_schema = {
    "type": "object",
    "properties": {
        "units": {"type": "string", "enum": ["metric", "imperial"], "default": "metric", "x-order": 1},
        "api_key": {"type": "string", "x-secret": True, "x-group": "Provider", "x-help": "From your provider's console."},
    },
}
```

Read it with `workspace.config.get("weather")`. Never write secrets anywhere
but this section.

## 5. A dashboard page

You may not need one. A captain whose tab has no compiled component gets the
generic captain page: your summary, a form that runs any of your commands, and
your recent jobs with their results. For many captains that is the whole UI:
make your commands return useful dicts.

When you need a real view, it is a React component registered under your tab
key in the dashboard build (`dashboard/app/lib/captains.tsx` for the
captains shipped in this repository). That means a pull request to this
repository or your own dashboard build; runtime-loaded UI for third-party
captains is not supported yet.

## 6. Routes and services

Add API routes when the dashboard or another system must call you directly.
Declare their prefixes in `route_prefixes`: Compass refuses writes under them
while your captain is off, and `compass validate` fails a route outside them.
Start long work through the queue rather than in the request:

```python
from compass_core.queue import queue_proxy
return queue_proxy.enqueue(captain="weather", command="forecast", payload={...})
```

A service is a loop `compass serve` runs while you are active: a callable
`(workspace_id, config, stop_event, *, interval)` that ticks, then waits on
`stop_event.wait(interval)`, and returns when the event is set. It is stopped
within a poll when someone turns your captain off.

## 7. Testing

Copy `tests/test_writing_a_captain.py`. It builds the open API with your
captain on `sys.path`, and checks the tab appears, the command runs, the config
round-trips, and turning the captain off refuses its writes. Run it with
`pytest` from a checkout of Compass, or vendor the fixture into your own
repository. `compass validate` belongs in your CI too.

## 8. Versions

- Your `version` (in the manifest and `pyproject.toml`) is what the Captains
  page shows and what the catalog compares to offer an update. Bump it on every
  release.
- `requires_core` is a PEP 440 range against the Compass you support:
  `>=0.3,<1` until Compass 1.0. A Compass outside the range lists your captain
  as incompatible and loads nothing of it.
- Data you write outlives an uninstall, and a later version must read what an
  earlier one wrote.

## 9. Publishing

1. Tag a release of your repository (`v0.1.0`), or publish to PyPI.
2. Check the install line works where it will run:
   ```
   pip install --target /tmp/check "compass-captain-weather@git+https://github.com/you/compass-captain-weather.git@v0.1.0"
   ```
3. Open a pull request to this repository adding your entry to
   `compass_core/catalog.json` (the format is in
   [writing_a_captain.md §10](writing_a_captain.md#10-publishing-to-the-catalog)).
   The pip line must be pinned: an exact version, a tag or commit, or a wheel
   with its sha256. A branch is rejected.

Once it is merged, every Compass lists your captain under Available within ten
minutes. An administrator installs it with one click and a restart, or by
adding the line to `COMPASS_EXTENSIONS`.

## 10. What a reviewer looks at

The catalog is a list of code that will run inside other people's Compass, with
their AWS and GitHub access. A reviewer checks that the summary says what the
captain does, the source is public and matches the tag, the pin is exact, the
captain asks for no more access than it uses, and `compass validate` passes.
Keep the summary honest and the permissions small and the review is short.
