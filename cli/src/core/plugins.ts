/**
 * Plugins: packages that add tools, commands and playbooks to the CLI and the MCP server.
 *
 * A plugin is an npm package whose default export (or `plugin` export) is a
 * CompassPlugin. The CLI loads, in order: the packages named in COMPASS_CLI_PLUGINS
 * (comma-separated), the `plugins` list in the CLI config file, and
 * `@beamreach/compass-enterprise` when it is installed. A plugin that cannot be
 * loaded is skipped with a warning on stderr; a tool whose name is already taken
 * is skipped too, so a plugin never replaces a built-in tool.
 */

import path from "node:path";
import type { Command } from "commander";
import type { ToolDef } from "./registry.js";

export interface AwsLoginOptions {
  stackName: string;
  region?: string;
  awsProfile?: string;
  url?: string;
  scope?: string;
  fetchImpl?: typeof fetch;
  log: (line: string) => void;
}

export interface AwsLoginResult {
  url: string;
  token: string;
  workspace_id?: string;
  region?: string;
}

export interface CompassPlugin {
  /** Shown by `compass plugins`. */
  name: string;
  version?: string;
  /** Tools: CLI commands and MCP tools at once. */
  tools?: ToolDef<any>[];
  /** Commands that are not tools (an installer, say). */
  registerCommands?: (program: Command, deps: unknown) => void;
  /** Backs `compass login --from-aws`: read a deployment's bootstrap secret and mint a token. */
  loginFromAws?: (options: AwsLoginOptions) => Promise<AwsLoginResult>;
  /** A directory of skills (<name>/SKILL.md), offered as MCP prompts beside the built-in ones. */
  skillsDir?: string;
}

export const DEFAULT_PLUGINS = ["@beamreach/compass-enterprise"];

export interface LoadedPlugins {
  plugins: CompassPlugin[];
  errors: Array<{ name: string; error: string }>;
}

function isPlugin(value: unknown): value is CompassPlugin {
  return !!value && typeof value === "object" && typeof (value as any).name === "string";
}

export async function loadPlugins(names?: string[], warn: (line: string) => void = () => undefined): Promise<LoadedPlugins> {
  const wanted: string[] = [];
  const add = (n: string) => {
    const name = n.trim();
    if (name && !wanted.includes(name)) wanted.push(name);
  };
  if (names) names.forEach(add);
  else {
    (process.env.COMPASS_CLI_PLUGINS || "").split(",").forEach(add);
    try {
      const { readConfig } = await import("./config.js");
      ((await readConfig()) as any).plugins?.forEach?.((n: string) => add(String(n)));
    } catch {
      // no config file yet
    }
    DEFAULT_PLUGINS.forEach(add);
  }
  const out: LoadedPlugins = { plugins: [], errors: [] };
  for (const name of wanted) {
    try {
      const spec = name.startsWith(".") || name.startsWith("/") ? path.resolve(name) : name;
      const mod: any = await import(spec);
      const plugin = mod.default ?? mod.plugin;
      if (!isPlugin(plugin)) throw new Error("does not export a Compass plugin");
      out.plugins.push(plugin);
    } catch (error: any) {
      const notInstalled = error?.code === "ERR_MODULE_NOT_FOUND" && String(error?.message || "").includes(name);
      if (notInstalled && DEFAULT_PLUGINS.includes(name) && !names) continue; // the default is optional
      out.errors.push({ name, error: error?.message || String(error) });
      warn(`compass: plugin ${name} not loaded: ${error?.message || error}`);
    }
  }
  return out;
}

/** The built-in tools plus every plugin's, a name taken once only. */
export function withPluginTools(builtIn: ToolDef<any>[], plugins: CompassPlugin[], warn: (line: string) => void = () => undefined): ToolDef<any>[] {
  const tools = [...builtIn];
  const taken = new Set(tools.map((t) => t.name));
  for (const plugin of plugins) {
    for (const tool of plugin.tools || []) {
      if (taken.has(tool.name)) {
        warn(`compass: ${plugin.name} tool ${tool.name} skipped: the name is taken`);
        continue;
      }
      taken.add(tool.name);
      tools.push(tool);
    }
  }
  return tools;
}
