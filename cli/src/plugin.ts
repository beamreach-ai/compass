/**
 * What a plugin imports: `import { defineTool, plan, type CompassPlugin } from "@beamreach/compass/plugin"`.
 * See src/core/plugins.ts for how plugins are found and loaded.
 */

export { defineTool, plan } from "./core/registry.js";
export type { ToolDef, ToolContext, Scope, CliSpec } from "./core/registry.js";
export type { CompassPlugin, AwsLoginOptions, AwsLoginResult } from "./core/plugins.js";
export { CompassClient } from "./core/client.js";
export type { RequestOptions } from "./core/client.js";
export * from "./core/errors.js";
export { readConfig, writeConfig, upsertProfile, configPath, resolveConnection } from "./core/config.js";
export type { Connection, Profile, ConfigFile } from "./core/config.js";
export * from "./core/poll.js";
export { CLI_VERSION } from "./version.js";
