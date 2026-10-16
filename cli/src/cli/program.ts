/** Build the commander program from the tool registry plus the local (non-API) commands. */

import { Command } from "commander";
import { z } from "zod";
import { CompassClient } from "../core/client.js";
import { resolveConnection, type Connection } from "../core/config.js";
import { CompassError, NotConfiguredError, PlanOnlyError } from "../core/errors.js";
import type { ToolContext, ToolDef } from "../core/registry.js";
import { TOOLS } from "../tools/index.js";
import { withPluginTools, type CompassPlugin } from "../core/plugins.js";
import { CLI_VERSION } from "../version.js";
import { registerLocalCommands } from "./local.js";
import { renderResult } from "./render.js";
import { coerce, flagSpecs, camel } from "./zodflags.js";

export interface ProgramDeps {
  fetchImpl?: typeof fetch;
  /** Override connection resolution (tests). */
  connection?: Connection | null;
  stdout?: (text: string) => void;
  stderr?: (text: string) => void;
  exit?: (code: number) => void;
  /** Loaded plugins (src/core/plugins.ts); bin.ts loads them, tests pass them. */
  plugins?: CompassPlugin[];
}

/** Global flags are declared on the root and on every leaf, so they work before or after the subcommand. */
export function addGlobalOptions(cmd: Command): Command {
  return cmd
    .option("--json", "machine-readable output")
    .option("--profile <name>", "profile from ~/.config/compass/config.json")
    .option("--url <url>", "Compass URL (overrides profile)")
    .option("--token <token>", "API token (overrides profile)")
    .option("--workspace <id>", "workspace id")
    .option("-y, --yes", "execute confirm-gated commands instead of printing the plan")
    .option("-q, --quiet", "suppress progress lines");
}

export interface GlobalOpts {
  json?: boolean;
  profile?: string;
  url?: string;
  token?: string;
  workspace?: string;
  yes?: boolean;
  quiet?: boolean;
}

export async function makeContext(globals: GlobalOpts, deps: ProgramDeps): Promise<ToolContext> {
  const connection =
    deps.connection !== undefined
      ? deps.connection
      : await resolveConnection({ profile: globals.profile, url: globals.url, token: globals.token, workspace: globals.workspace });
  let client: CompassClient | undefined;
  return {
    connection,
    client: () => {
      if (!client) {
        if (!connection) throw new NotConfiguredError();
        client = new CompassClient(connection, deps.fetchImpl ?? fetch);
      }
      return client;
    },
    log: (line) => {
      if (!globals.quiet) (deps.stderr || ((t) => process.stderr.write(t + "\n")))(line);
    },
  };
}

function ensureGroup(root: Command, path: string[]): Command {
  let current = root;
  for (const segment of path) {
    let next = current.commands.find((c) => c.name() === segment);
    if (!next) {
      next = current.command(segment).description(`${segment} commands`);
    }
    next.enablePositionalOptions();
    current = next;
  }
  return current;
}

export async function runTool(tool: ToolDef<any>, args: Record<string, unknown>, globals: GlobalOpts, deps: ProgramDeps): Promise<unknown> {
  const ctx = await makeContext(globals, deps);
  if (tool.confirm || "confirm" in tool.input) args.confirm = args.confirm ?? !!globals.yes;
  const parsed = z.object(tool.input).parse(args);
  return tool.handler(parsed, ctx);
}

export function buildProgram(deps: ProgramDeps = {}): Command {
  const out = deps.stdout || ((t: string) => process.stdout.write(t + "\n"));
  const err = deps.stderr || ((t: string) => process.stderr.write(t + "\n"));
  const exit = deps.exit || ((code: number) => process.exit(code));

  const program = addGlobalOptions(new Command("compass"))
    .description("Compass: configure and operate Compass from a terminal or an AI agent.")
    .version(CLI_VERSION)
    .enablePositionalOptions()
    .showHelpAfterError();

  const plugins = deps.plugins || [];
  for (const tool of withPluginTools(TOOLS, plugins, err)) {
    const path = tool.cli.command.split(" ");
    const leaf = path.pop()!;
    const parent = ensureGroup(program, path);
    const positionals = tool.cli.positionals || [];
    const specs = flagSpecs(tool.input, ["confirm"]);
    const existingCmd = parent.commands.find((c) => c.name() === leaf);
    const cmd = (existingCmd || parent.command(leaf)).description(tool.cli.summary || tool.description.split(". ")[0]);
    // A node that is both a leaf (e.g. `install`) and a group (`install status`) must not consume
    // options that belong to its subcommands: `install check-prereqs --region x` is the subcommand's --region.
    cmd.enablePositionalOptions();

    const posSpecs = positionals.map((key) => specs.find((s) => s.key === key)).filter(Boolean) as typeof specs;
    for (const spec of posSpecs) {
      const variadic = spec.kind === "string[]";
      cmd.argument(`[${spec.key}${variadic ? "..." : ""}]`, spec.description);
    }
    for (const spec of specs) {
      if (positionals.includes(spec.key)) continue;
      cmd.option(spec.flag, spec.description);
    }
    if (!cmd.options.some((o) => o.long === "--json")) addGlobalOptions(cmd);
    cmd.addHelpText("after", `\nMCP tool: ${tool.name} (scope ${tool.scope})\n${tool.description}`);

    cmd.action(async (...actionArgs: unknown[]) => {
      const command = actionArgs[actionArgs.length - 1] as Command;
      const options = actionArgs[actionArgs.length - 2] as Record<string, unknown>;
      const positionalValues = actionArgs.slice(0, -2);
      const globals = command.optsWithGlobals<GlobalOpts>();
      const args: Record<string, unknown> = {};
      posSpecs.forEach((spec, index) => {
        const value = positionalValues[index];
        if (value !== undefined && !(Array.isArray(value) && value.length === 0)) args[spec.key] = coerce(spec, value);
      });
      for (const spec of specs) {
        if (positionals.includes(spec.key)) continue;
        const value = options[camel(spec.key)];
        if (value !== undefined) args[spec.key] = coerce(spec, value);
      }
      try {
        const result = await runTool(tool, args, globals, deps);
        if (globals.json) {
          out(JSON.stringify(result, null, 2));
        } else {
          out(tool.render ? tool.render(result) : renderResult(result));
        }
        const isPlan = !!(result && typeof result === "object" && (result as any).executed === false && (result as any).plan);
        if (isPlan && !globals.json) exit(new PlanOnlyError().exitCode);
        else if (tool.exitCode) {
          const code = tool.exitCode(result);
          if (code) exit(code);
        }
      } catch (error: any) {
        if (error instanceof z.ZodError) {
          err(`Invalid arguments:\n${error.issues.map((i) => `  ${i.path.join(".") || "(input)"}: ${i.message}`).join("\n")}`);
          command.outputHelp({ error: true });
          exit(1);
          return;
        }
        if (error instanceof CompassError) {
          if (globals.json) out(JSON.stringify({ error: error.code, message: error.message, details: error.details ?? null }, null, 2));
          else err(`error (${error.code}): ${error.message}`);
          exit(error.exitCode);
          return;
        }
        if (globals.json) out(JSON.stringify({ error: "unexpected", message: error?.message || String(error) }, null, 2));
        else err(`error: ${error?.message || String(error)}`);
        exit(1);
      }
    });
  }

  registerLocalCommands(program, deps);
  for (const plugin of plugins) plugin.registerCommands?.(program, deps);
  return program;
}
