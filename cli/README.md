# @beamreach/compass

Operate Compass from a terminal or from an AI agent. One package, three entry points:

- `compass <verb>`: the CLI.
- `compass mcp`: an MCP server over stdio, exposing the same verbs as tools.
- `compass agent install`: registers the MCP server and the skills with Claude Code.

## Install

```
npm i -g @beamreach/compass
compass login --url http://localhost:8080 --token cmp_…   # a token from Settings → API tokens
compass agent install                                       # optional: Claude Code gets the tools and skills
compass status
```

The open Compass runs with Docker Compose (see the repository's quickstart);
this CLI talks to its API.

## Everyday verbs

```
compass status                       # one-screen brief
compass whoami                       # who this connection is: id, roles, teams, source
compass map get --search bucket      # the infra map
compass iac coverage                 # what Terraform manages
compass iac import <ids> --yes       # import into Terraform via a pull request
compass honeypots suggest            # where decoys would look real
compass honeypots plant --top-n 3 --yes
compass prs list                     # the pull requests Compass opened
compass captains list                # captains: what is installed and on
compass captains catalog             # what can be installed
compass extensions install hello --yes && compass extensions restart --yes
compass jobs list
```

Every verb takes `--json`. Verbs that change something print a plan and exit 2
unless given `--yes`.

## Scopes

API tokens have a scope: `read`, `operate` (decoys, IaC, pull requests,
notifications) or `admin` (settings, tokens, captains).

## Plugins

A plugin is an npm package that adds tools (CLI verbs and MCP tools at once),
commands and skills. The CLI loads the packages named in `COMPASS_CLI_PLUGINS`
(comma-separated), those listed under `"plugins"` in the config file, and
`@beamreach/compass-enterprise` when it is installed. `compass plugins` lists
what is loaded.

```ts
// index.ts of a plugin package
import { defineTool, type CompassPlugin } from "@beamreach/compass/plugin";
import { z } from "zod";

const hello = defineTool({
  name: "compass_hello",
  description: "Say hello through the hello captain.",
  input: { name: z.string() },
  scope: "operate",
  cli: { command: "hello", positionals: ["name"] },
  handler: async (args, ctx) => ctx.client().post("/api/jobs", { captain: "hello", command: "greet", payload: { name: args.name } }),
});

const plugin: CompassPlugin = { name: "compass-hello", tools: [hello] };
export default plugin;
```

A plugin cannot replace a built-in tool; a tool whose name is taken is skipped
with a warning.

## Configuration

Profiles live in `~/.config/compass/config.json` (or `$COMPASS_CLI_CONFIG`).
`COMPASS_URL`, `COMPASS_TOKEN`, `COMPASS_PROFILE` and `COMPASS_WORKSPACE`
override the profile.

## Development

```
npm install
npm test
npm run typecheck
npm run build
```
