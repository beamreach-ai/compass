# Writing a captain

A captain is the unit people install, turn on or off, and configure
(`docs/captain_extensions_spec.md`). It is a Python package with one manifest;
the manifest is the only place the rest of Compass learns about it from. The
smallest complete one is `tests/fixtures/captain_hello/`, and
`tests/test_writing_a_captain.py` drives it through everything below.

## 1. The manifest

```python
# mycaptain/manifest.py
from compass_core.captains import CaptainManifest, ServiceSpec, TabSpec

MANIFEST = CaptainManifest(
    name="hello",                       # the dispatch key and the config section; never changes once shipped
    label="Hello",
    summary="Says hello.",              # one line on the Captains page
    tier="open",                        # open | cloud | enterprise (compass_core.licence decides what a workspace may run)
    feature="hello",                    # the DISABLED_FEATURES key an operator can veto it with
    commands="mycaptain.commands",      # importing this module registers the (captain, command) handlers
    routers=("mycaptain.routes:router",),
    route_prefixes=("/api/hello",),     # paths that are yours: writes under them are refused while you are off
    tabs=(TabSpec(key="hello", label="Hello", group="platform", feature="hello"),),
    queue="hello-executions",           # the product's Celery queue; `compass serve` ignores it
    services=(ServiceSpec(name="poll", start="mycaptain.service:run_service", interval=60, interval_setting="hello_poll"),),
    config_schema={...},                # JSON Schema of your section of the workspace config (§4)
    depends_on=("mapper",),             # captains that must be on for you to be on
    requires_core=">=0.1,<1",           # PEP 440 against compass_core.__version__
    version="0.1.0",
)
```

`MANIFEST.errors()` lists what is wrong with it; the registry refuses a manifest
with errors and logs why.

## 2. Discovery

Declare an entry point in your distribution:

```toml
[project.entry-points."compass.captains"]
hello = "mycaptain.manifest:MANIFEST"
```

`pip install` it next to Compass and it is found. In a checkout that is not
installed, `COMPASS_CAPTAINS=mycaptain.manifest:MANIFEST` (comma-separated for
several) does the same. `compass captains` lists what was found; a manifest
whose `requires_core` rejects this core is listed as incompatible and nothing
of it is loaded.

## 3. Commands

```python
# mycaptain/commands.py
from compass_core import dispatch
from compass_core.workspace import resolve

def greet(payload):
    workspace = resolve(payload)        # the workspace id is in the payload; its config comes with it
    return {"greeting": "hello"}

COMMANDS = {"greet": greet}
dispatch.register("hello", COMMANDS)
```

A command takes a payload dict and returns a JSON-able dict. It runs the same
way from the shell (`compass hello greet --workspace ws`), from a job the API
queued (`compass serve` or a product worker), and from another captain's
follow-up (`compass_core.queue.job_queue().enqueue(captain="hello", command="greet", payload=...)`).
The dispatcher refuses a job for a captain that is off; you do not check.

## 4. Configuration

Your settings live in the workspace config under your `name` (or
`config_section`). Declare their shape and the Captains page draws the form,
validates a `PUT /api/captains/hello/config`, masks secrets and keeps them when
the mask is sent back:

```python
config_schema = {
    "type": "object",
    "properties": {
        "greeting": {"type": "string", "default": "hello", "x-order": 1, "x-help": "What to say."},
        "api_key": {"type": "string", "x-secret": True, "x-group": "Credentials"},
    },
}
```

Supported: string, integer, number, boolean, enum, array of strings, one level
of object. `x-order` sorts, `x-group` makes a fieldset, `x-help` is the line
under the field, `x-secret` masks. Anything richer is your own React component,
named by `settings_component` and registered in the dashboard
(`dashboard/app/lib/captains.tsx` for the open ones).

## 5. Routes and the tab

Your routers are included in the order you list them, after the core's. Any
request under your `route_prefixes` is checked against your state: reads pass,
other methods get `403 {"code": "captain_disabled"}` (or `402 captain_unlicensed`)
while you are off. Start work through `compass_core.queue.queue_proxy.enqueue`
so the API returns at once and a worker runs it.

A tab needs a React component registered under the same key in the dashboard
build (the open ones in `dashboard/app/lib/captains.tsx`). The sidebar draws the
tab from the bootstrap: label, group (`cost`, `security`, `platform`) and
sub-views come from your `TabSpec`.

## 6. Services

A `ServiceSpec` names a callable `(workspace_id, config, stop_event, *, interval)`
that `compass serve` runs in a thread while you are active. Tick first, then
`stop_event.wait(interval)`, and return when it is set; a server in `once` mode
hands you a set event so you tick exactly once. `interval_setting` names the
serve payload key or `COMPASS_<SETTING>` variable that overrides the interval;
0 means the service is not started.

## 7. Installing it into the open Compass

The open image installs extensions on start. In the compose file's `.env`:

```
COMPASS_EXTENSIONS=mycaptain==0.1.0
```

Any pip requirement works: a name on PyPI, `git+https://…`, or a wheel you
drop into `./extensions/wheels/`. The entrypoint installs the list into
`./extensions` (a volume, so a restart keeps it and only a changed list
installs again), and your `compass.captains` entry point is found from there.
`compass captains` (or Settings → Captains) then lists you, on by default.

## 8. A page without a dashboard build

A captain's tab needs a React component only when it wants one. For a key the
dashboard build has no component for, the shell renders the generic captain
page: your summary, a "Run a command" form over your commands (`POST /api/jobs`
with `{captain, command, payload}`), and your recent jobs. The first line of
each command's docstring is its description in that form. That is enough for a
captain that does its work in commands and reports through the job's result;
a richer view is a component registered under your key in the build.

## 9. The test

Copy `tests/test_writing_a_captain.py`: set `COMPASS_CAPTAINS` to your manifest,
build the open app, and assert your tab is in the bootstrap, your command runs,
your config round-trips, and turning you off refuses your writes and hides
your tab. `tests/test_captain_manifests.py` runs over every discovered manifest,
so the structural checks (unique names and tab keys, resolvable routers and
services, valid schema, prefixes covering your routes) are free.
