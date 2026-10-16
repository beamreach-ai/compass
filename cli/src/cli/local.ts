/** Commands that are not API tools: login, profile, agent install, mcp. */

import { spawnSync } from "node:child_process";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { Command } from "commander";
import { CompassClient } from "../core/client.js";
import { configPath, readConfig, removeProfile, upsertProfile, writeConfig } from "../core/config.js";
import { CompassError } from "../core/errors.js";
import { addGlobalOptions, type ProgramDeps } from "./program.js";

function pkgRoot(): string {
  // dist/bin.js -> package root
  const here = path.dirname(new URL(import.meta.url).pathname);
  return path.resolve(here, "..", "..");
}

export function registerLocalCommands(program: Command, deps: ProgramDeps): void {
  const out = deps.stdout || ((t: string) => process.stdout.write(t + "\n"));
  const err = deps.stderr || ((t: string) => process.stderr.write(t + "\n"));
  const exit = deps.exit || ((code: number) => process.exit(code));

  addGlobalOptions(program.command("login"))
    .description("Save a Compass profile: --url and --token, or --from-aws to bootstrap from the stack's secret")
    .option("--name <profile>", "profile name (default: host of the URL)")
    .option("--from-aws", "read the bootstrap token from Secrets Manager and mint a personal token (needs AWS credentials)")
    .option("--stack-name <name>", "stack name for --from-aws (default compass)")
    .option("--region <region>", "AWS region for --from-aws")
    .option("--aws-profile <name>", "AWS profile for --from-aws")
    .option("--scope <scope>", "scope of the minted token for --from-aws (read|operate|admin)", "admin")
    .action(async (opts, command: Command) => {
      try {
        const globals = command.optsWithGlobals<{ url?: string; token?: string; workspace?: string }>();
        let url: string | undefined = globals.url;
        let token: string | undefined = globals.token;
        let workspace: string | undefined = globals.workspace;
        const extra: Record<string, unknown> = {};
        if (opts.fromAws) {
          const provider = (deps.plugins || []).find((p) => p.loginFromAws);
          if (!provider?.loginFromAws) {
            throw new CompassError("usage", "--from-aws reads a Compass Enterprise deployment's secret: install @beamreach/compass-enterprise, or use --url and --token.", 1);
          }
          const result = await provider.loginFromAws({
            stackName: opts.stackName || "compass",
            region: opts.region,
            awsProfile: opts.awsProfile,
            url: globals.url,
            scope: opts.scope,
            fetchImpl: deps.fetchImpl,
            log: (line) => err(line),
          });
          url = result.url;
          token = result.token;
          workspace = workspace || result.workspace_id;
          Object.assign(extra, { stack_name: opts.stackName || "compass", region: result.region, aws_profile: opts.awsProfile });
        }
        if (!url || !token) throw new CompassError("usage", "Provide --url and --token, or --from-aws.", 1);
        const client = new CompassClient({ url, token, workspace_id: workspace }, deps.fetchImpl);
        const me = await client.get("/api/auth/me");
        if (me?.method !== "api_token") throw new CompassError("auth", "Compass did not recognise the token (engine may predate API tokens).", 6);
        const name = opts.name || new URL(url).host.replace(/[^a-z0-9.-]/gi, "-");
        await upsertProfile(name, { url, token, workspace_id: workspace || me.workspace_id, ...extra });
        out(`Saved profile '${name}' (${me.kind} token, scope ${me.scope}, workspace ${workspace || me.workspace_id}) in ${configPath()}`);
      } catch (error: any) {
        err(`error: ${error.message}`);
        exit(error instanceof CompassError ? error.exitCode : 1);
      }
    });

  const profile = program.command("profile").description("Manage saved Compass profiles");
  profile.command("list").description("List profiles").action(async () => {
    const config = await readConfig();
    const names = Object.keys(config.profiles);
    if (!names.length) return out(`No profiles in ${configPath()}. Run \`compass login\`.`);
    for (const name of names) {
      const p = config.profiles[name];
      out(`${name === config.current ? "*" : " "} ${name}\t${p.url}\t${p.workspace_id || ""}${p.stack_name ? `\tstack ${p.stack_name}` : ""}`);
    }
  });
  profile.command("use <name>").description("Make a profile current").action(async (name: string) => {
    const config = await readConfig();
    if (!config.profiles[name]) {
      err(`No profile '${name}'`);
      return exit(1);
    }
    config.current = name;
    await writeConfig(config);
    out(`Current profile: ${name}`);
  });
  profile.command("remove <name>").description("Delete a profile").action(async (name: string) => {
    out((await removeProfile(name)) ? `Removed profile '${name}'` : `No profile '${name}'`);
  });

  const agent = program.command("agent").description("Register Compass with an AI agent client");
  agent
    .command("install")
    .description("Register the MCP server (and skills, for Claude Code) with a client")
    .option("--client <name>", "claude-code | claude-desktop | cursor | print", "claude-code")
    .option("--scope <scope>", "claude-code: user | project | local", "user")
    .action(async (opts) => {
      const command = process.argv[1] && /\.c?js$/.test(process.argv[1]) ? ["node", process.argv[1], "mcp"] : ["compass", "mcp"];
      const entry = { command: command[0], args: command.slice(1) };
      if (opts.client === "claude-code") {
        const result = spawnSync("claude", ["mcp", "add", "--scope", opts.scope, "--transport", "stdio", "compass", "--", ...command], { stdio: "inherit" });
        if (result.error || result.status !== 0) {
          err("Could not run `claude mcp add`. Is Claude Code installed? Manual config below.");
          out(JSON.stringify({ mcpServers: { compass: entry } }, null, 2));
          return exit(1);
        }
        const skillsDir = path.join(os.homedir(), ".claude", "skills");
        const bundled = path.join(pkgRoot(), "plugin", "skills");
        try {
          const entries = await fs.readdir(bundled);
          await fs.mkdir(skillsDir, { recursive: true });
          for (const name of entries) {
            await fs.cp(path.join(bundled, name), path.join(skillsDir, name), { recursive: true, force: true });
          }
          out(`Installed MCP server 'compass' and ${entries.length} skill(s) into ${skillsDir}.`);
        } catch {
          out("Installed MCP server 'compass'. (No bundled skills found.)");
        }
        return;
      }
      if (opts.client === "claude-desktop") {
        const file =
          process.platform === "darwin"
            ? path.join(os.homedir(), "Library", "Application Support", "Claude", "claude_desktop_config.json")
            : process.platform === "win32"
              ? path.join(process.env.APPDATA || "", "Claude", "claude_desktop_config.json")
              : path.join(os.homedir(), ".config", "Claude", "claude_desktop_config.json");
        let config: any = {};
        try {
          config = JSON.parse(await fs.readFile(file, "utf8"));
        } catch {
          config = {};
        }
        config.mcpServers = { ...(config.mcpServers || {}), compass: entry };
        await fs.mkdir(path.dirname(file), { recursive: true });
        await fs.writeFile(file, JSON.stringify(config, null, 2));
        out(`Added 'compass' to ${file}. Restart Claude Desktop.`);
        return;
      }
      if (opts.client === "cursor") {
        const file = path.join(os.homedir(), ".cursor", "mcp.json");
        let config: any = {};
        try {
          config = JSON.parse(await fs.readFile(file, "utf8"));
        } catch {
          config = {};
        }
        config.mcpServers = { ...(config.mcpServers || {}), compass: entry };
        await fs.mkdir(path.dirname(file), { recursive: true });
        await fs.writeFile(file, JSON.stringify(config, null, 2));
        out(`Added 'compass' to ${file}.`);
        return;
      }
      out(JSON.stringify({ mcpServers: { compass: entry } }, null, 2));
      try {
        const skills = await fs.readdir(path.join(pkgRoot(), "plugin", "skills"));
        out(`Skills bundled (installed into ~/.claude/skills by --client claude-code; served as MCP prompts to every client): ${skills.join(", ")}`);
      } catch {
        /* no skills dir */
      }
    });
  agent
    .command("uninstall")
    .description("Remove the MCP registration (Claude Code) and bundled skills")
    .action(async () => {
      spawnSync("claude", ["mcp", "remove", "compass"], { stdio: "inherit" });
      const skillsDir = path.join(os.homedir(), ".claude", "skills");
      try {
        const bundled = await fs.readdir(path.join(pkgRoot(), "plugin", "skills"));
        for (const name of bundled) await fs.rm(path.join(skillsDir, name), { recursive: true, force: true });
      } catch {
        /* nothing bundled */
      }
      out("Removed Compass from Claude Code.");
    });

  program
    .command("plugins")
    .description("List the loaded plugins and what they add")
    .action(() => {
      const plugins = deps.plugins || [];
      if (!plugins.length) {
        out("No plugins loaded. Name packages in COMPASS_CLI_PLUGINS or under \"plugins\" in the config file.");
        return;
      }
      for (const p of plugins) out(`${p.name}${p.version ? ` ${p.version}` : ""}: ${(p.tools || []).length} tools${p.loginFromAws ? ", login --from-aws" : ""}${p.skillsDir ? ", skills" : ""}`);
    });

  program
    .command("mcp")
    .description("Run the MCP server on stdio (what agents connect to)")
    .action(async () => {
      const { runMcpServer } = await import("../mcp/server.js");
      await runMcpServer(deps);
    });
}
