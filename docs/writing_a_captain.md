# Writing a captain

The step-by-step path, from an empty directory to the catalog, is the
[captain development guide](captain_development.md). This page is the
reference for the manifest and what each part does.

A captain is the unit people install, turn on or off, and configure
(`docs/captain_extensions_spec.md`). It is a Python package with one manifest;
the manifest is the only place the rest of Compass learns about it from. The
smallest complete one is `examples/captain-hello/`, a pip-installable package:
copy that directory to start. `tests/test_writing_a_captain.py` drives it
through everything below.

## 1. The manifest

```python
# mycaptain/manifest.py
from compass_core.captains import AwsPermissions, CaptainManifest, Permissions, ServiceSpec, Statement, TabSpec

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
    permissions=Permissions(            # what you read in AWS and where else you reach (§1.1)
        aws=AwsPermissions(read=(Statement(actions=("ecs:Describe*", "ecs:List*")),)),
        github=("contents:read",),
        egress=("api.github.com",),
    ),
    requires_core=">=0.1,<1",           # PEP 440 against compass_core.__version__
    version="0.1.0",
)
```

`MANIFEST.errors()` lists what is wrong with it; the registry refuses a manifest
with errors and logs why.

### 1.1 Permissions

Compass only reads a target account, and only what each captain declared. Your
`permissions.aws.read` is the list of IAM read actions your code calls, with `*`
wildcards per service (`ecs:Describe*`) so a new describe call needs no change.
Three things come from it and nothing is written by hand: the read role's policy
(the union over the installed captains), your sessions' inline policy (STS
narrows every session you get from `compass_core.sessions.target("read")` to
exactly this block, whatever the role allows), and the check that your code
calls only what you declared. `compass validate` runs that check: a static pass
over your package for `.client("<service>")` and `<verb>_<noun>(` pairs, mapped
to IAM action names; a call the manifest does not cover fails with the file and
line. The rules: read-only verbs only (`Get`, `List`, `Describe`, `Lookup`, …; a
write is an *action*, which a person grants by pull request), no `*:*`, no
`s3:GetObject` on `*` (name the bucket: `{state_bucket}` is the configured state
bucket), no `lambda:Get*` (it downloads code). `github` and `egress` are shown
before install; nothing enforces them yet. A captain that reads nothing declares
`Permissions()`: without the field, `compass validate` fails.

A write is an **action**: a class deriving `compass_core.actions.Action` with
the IAM statements it needs for a scope, a scope check, and the four steps
(snapshot, execute, verify, rollback), registered by your commands module with
`compass_core.actions.register("mycaptain", {"mycaptain.freeze": Freeze})` and
named in `permissions.aws.actions`. Proposing one is all a captain can do: a
person accepts it, Compass opens the pull request that grants exactly those
statements to the act role, and the action runs only once that is merged,
applied and verified. The Captains page shows the block before install as
"Will use": reads by service, the actions with their change type and rollback,
GitHub scopes, egress. A catalog entry may carry the same block (`permissions`,
in the shape the page shows) so it is visible before the package is downloaded.

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

## 6.1 Inbox items

Anything your captain needs a person for is an inbox item: a decision to take,
a repair to make, a merge to do. The inbox (`compass_core.inbox`,
[docs/inbox.md](inbox.md)) derives the list from state on every request, so you
store nothing: write a function that reads your rows and returns the items
that exist right now, and name it in the manifest.

```python
# captains/finops/inbox.py
from compass_core.inbox import Action, Context, Item

def items(ctx: Context) -> list[Item]:
    return [Item(id=f"finops.fix:{row['id']}", kind="finops.fix", verb="approve", severity="normal",
                 captain="finops", tab="finops", title=f"Downsize {row['name']}", summary=f"saves ${row['saving']}/month",
                 since=row["found_at"], subject={"fix_id": row["id"]},
                 actions=[Action(key="apply", label="Apply", method="POST", path=f"/api/finops/fixes/{row['id']}/apply",
                                 scope="operate", confirm=f"Resizes {row['name']}."),
                          Action(key="open", label="Open", href=f"/workspace?tab=finops&fix={row['id']}")],
                 detail={"before": row["before"], "after": row["after"]})
            for row in open_fixes(ctx.table, ctx.pk)]
```

```python
MANIFEST = CaptainManifest(..., inbox="captains.finops.inbox:items")
```

The id is deterministic (`<kind>:<subject>`), `since` is when the state began
(epoch ms; the feed is newest first), `actions` are the routes your tab already
calls (the dashboard, the CLI and the MCP tool render them and know nothing
about your kind), and `detail` is what your tab shows before the same click,
as data. A function that raises is logged and skipped; it never breaks the
list. A captain that is not a distribution of its own registers the same
function with `compass_api.extensions.register_inbox_source(name, fn)`.

## 7. Installing it into the open Compass

From the catalog: Settings → Captains → Available → Install, then Restart
Compass. The API and the daemon exit, compose starts them again, and the
image's entrypoint installs the new set before Compass starts. Uninstall and
update work the same way. `compass install captain=<name>` and
`compass uninstall captain=<name>` do the same from a shell in the container.

Outside the catalog, name it in `.env`:

```
COMPASS_EXTENSIONS=mycaptain==0.1.0 other@git+https://github.com/you/other.git@v1.0.0
```

Requirements have no spaces (`name@url`, not `name @ url`) and are separated
by spaces or commas. A wheel you drop into `./extensions/wheels/` can be named
too. Both lists are installed into `./extensions/site`, rebuilt only when the
set changes; a failed install keeps the previous set and the Captains page
shows the error.

Operator settings:

| Variable | Effect |
|---|---|
| `COMPASS_CATALOG_URL` | Where the catalog is read from; `off` hides the Available tab |
| `COMPASS_CATALOG_INSTALL=off` | The page lists the catalog but does not install; `.env` still works |
| `COMPASS_CATALOG_ALLOW` | Only these captain names may be installed from the page |
| `COMPASS_SELF_RESTART=1` | Set by the shipped compose file: the page may restart Compass |

## 8. A page without a dashboard build

A captain's tab needs a React component only when it wants one. For a key the
dashboard build has no component for, the shell renders the generic captain
page: your summary, a "Run a command" form over your commands (`POST /api/jobs`
with `{captain, command, payload}`), and your recent jobs. The first line of
each command's docstring is its description in that form. That is enough for a
captain that does its work in commands and reports through the job's result;
a richer view is a component registered under your key in the build.

## 9. The test

Copy `tests/test_writing_a_captain.py`: put your package on `sys.path`, set `COMPASS_CAPTAINS` to your manifest,
build the open app, and assert your tab is in the bootstrap, your command runs,
your config round-trips, and turning you off refuses your writes and hides
your tab. `tests/test_captain_manifests.py` runs over every discovered manifest,
so the structural checks (unique names and tab keys, resolvable routers and
services, valid schema, prefixes covering your routes) are free.

## 10. Publishing to the catalog

Settings → Captains → Available lists the captain catalog, one JSON file in the
public repository (`compass_core/catalog.json`). Every Compass reads it from
`main` (or `COMPASS_CATALOG_URL`), and falls back to the copy it shipped with.
To list your captain, open a pull request adding an entry:

```json
{
  "name": "hello",
  "label": "Hello",
  "summary": "One or two sentences: what it does for the person who installs it.",
  "author": "You",
  "homepage": "https://… (docs)",
  "source": "https://… (the code)",
  "tier": "open",
  "tags": ["example"],
  "version": "0.1.0",
  "requires_core": ">=0.2,<1",
  "pip": "your-distribution@git+https://github.com/you/repo.git@v0.1.0"
}
```

`name`, `version` and `requires_core` must match your manifest. `pip` is one
requirement without spaces, pinned: `name==1.2.3`, a git tag or commit
(`name@git+https://…@v1.2.3`), or a wheel URL with `#sha256=`. A branch is
rejected: what someone installs from the catalog must be what was reviewed. `tests/compass_core/test_core_catalog.py` validates
every entry, so the pull request's CI says whether it is well formed.
