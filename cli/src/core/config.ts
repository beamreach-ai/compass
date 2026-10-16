/**
 * Profiles live in ~/.config/compass/config.json (or $COMPASS_CLI_CONFIG).
 * Precedence for the active connection: flags > env > current profile.
 */

import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";

export interface Profile {
  url: string;
  token?: string;
  workspace_id?: string;
  aws_profile?: string;
  region?: string;
  stack_name?: string;
  install_path?: "direct" | "marketplace" | "local";
  /** local installs: directory holding docker-compose.yml and .env */
  local_dir?: string;
  /** Things the installer created outside the stack, so uninstall never guesses. */
  created?: {
    network_stack?: string;
    dns_record?: { zone_id: string; name: string };
    target_role_stacks?: Record<string, string>;
    alert_stack?: string;
  };
}

export interface ConfigFile {
  current?: string;
  profiles: Record<string, Profile>;
  /** Plugin packages to load (src/core/plugins.ts). */
  plugins?: string[];
}

export function configPath(): string {
  // COMPASS_CLI_CONFIG, not COMPASS_CONFIG: in the open repository that names the workspace config file
  if (process.env.COMPASS_CLI_CONFIG) return process.env.COMPASS_CLI_CONFIG;
  const base = process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config");
  return path.join(base, "compass", "config.json");
}

export async function readConfig(): Promise<ConfigFile> {
  try {
    const raw = await fs.readFile(configPath(), "utf8");
    const parsed = JSON.parse(raw) as ConfigFile;
    if (!parsed.profiles) parsed.profiles = {};
    return parsed;
  } catch (err: any) {
    if (err?.code === "ENOENT") return { profiles: {} };
    throw err;
  }
}

export async function writeConfig(config: ConfigFile): Promise<void> {
  const file = configPath();
  await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  await fs.writeFile(file, JSON.stringify(config, null, 2) + "\n", { mode: 0o600 });
  await fs.chmod(file, 0o600).catch(() => undefined);
}

export async function upsertProfile(name: string, profile: Profile, makeCurrent = true): Promise<void> {
  const config = await readConfig();
  config.profiles[name] = { ...(config.profiles[name] || {}), ...profile };
  if (makeCurrent || !config.current) config.current = name;
  await writeConfig(config);
}

export async function removeProfile(name: string): Promise<boolean> {
  const config = await readConfig();
  if (!config.profiles[name]) return false;
  delete config.profiles[name];
  if (config.current === name) config.current = Object.keys(config.profiles)[0];
  await writeConfig(config);
  return true;
}

export interface Connection {
  url: string;
  token?: string;
  workspace_id?: string;
  profileName?: string;
  profile?: Profile;
}

export interface ConnectionOverrides {
  url?: string;
  token?: string;
  profile?: string;
  workspace?: string;
}

/** Resolve the connection the CLI/MCP should talk to. Returns null when nothing is configured. */
export async function resolveConnection(overrides: ConnectionOverrides = {}): Promise<Connection | null> {
  const env = process.env;
  const config = await readConfig();
  const profileName = overrides.profile || env.COMPASS_PROFILE || config.current;
  const profile = profileName ? config.profiles[profileName] : undefined;

  const url = overrides.url || env.COMPASS_URL || profile?.url;
  if (!url) return null;
  return {
    url: url.replace(/\/+$/, ""),
    token: overrides.token || env.COMPASS_TOKEN || profile?.token,
    workspace_id: overrides.workspace || env.COMPASS_WORKSPACE || profile?.workspace_id,
    profileName: profile ? profileName : undefined,
    profile,
  };
}
