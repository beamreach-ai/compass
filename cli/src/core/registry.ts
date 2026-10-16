/**
 * One registry drives both the CLI and the MCP server.
 * A tool is a verb over the Compass API (or local AWS) with a zod input schema.
 */

import type { z } from "zod";
import type { CompassClient } from "./client.js";
import type { Connection } from "./config.js";

export type Scope = "read" | "operate" | "admin";

export interface ToolContext {
  /** Lazily built; throws NotConfiguredError when no profile/URL is available. */
  client: () => CompassClient;
  connection: Connection | null;
  /** Progress lines for humans (CLI stderr, MCP logging). */
  log: (line: string) => void;
  /** Non-interactive by default; the CLI may provide a prompt. */
  prompt?: (question: string, choices?: string[]) => Promise<string>;
}

export interface CliSpec {
  /** Space separated path, e.g. "findings list". */
  command: string;
  /** Schema keys taken from positional args, in order. */
  positionals?: string[];
  /** Short aliases for flags: schema key -> single letter. */
  aliases?: Record<string, string>;
  summary?: string;
}

export interface ToolDef<S extends z.ZodRawShape = z.ZodRawShape> {
  name: string;
  description: string;
  /** zod raw shape; MCP uses it directly, the CLI derives flags from it. */
  input: S;
  scope: Scope;
  /** The captain this tool drives (compass_core.captains); the MCP server hides the tool while that captain is off. */
  captain?: string;
  annotations?: {
    readOnlyHint?: boolean;
    destructiveHint?: boolean;
    idempotentHint?: boolean;
    openWorldHint?: boolean;
  };
  /** When true the tool has a `confirm` input and returns a plan unless confirm=true. */
  confirm?: boolean;
  /** Output includes material the agent must not echo (lures, tokens, presigned URLs). */
  sensitive?: boolean;
  cli: CliSpec;
  /** Optional human renderer; default is a generic table/kv printer. */
  render?: (result: unknown) => string;
  /** Optional exit code derived from a successful result (e.g. doctor: 1 when a check failed). */
  exitCode?: (result: unknown) => number;
  handler: (args: z.infer<z.ZodObject<S>>, ctx: ToolContext) => Promise<unknown>;
}

export function defineTool<S extends z.ZodRawShape>(def: ToolDef<S>): ToolDef<S> {
  return def;
}

/** Marker in results for confirm-gated tools that did not execute. */
export interface PlanResult {
  executed: false;
  plan: {
    summary: string;
    steps: string[];
    warnings?: string[];
    [key: string]: unknown;
  };
}

export function plan(summary: string, steps: string[], extra: Record<string, unknown> = {}, warnings?: string[]): PlanResult {
  return { executed: false, plan: { summary, steps, ...(warnings ? { warnings } : {}), ...extra } };
}
