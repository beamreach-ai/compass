/** MCP server over the same tool registry the CLI uses. */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { CompassClient } from "../core/client.js";
import { resolveConnection, type Connection } from "../core/config.js";
import { CompassError, NotConfiguredError } from "../core/errors.js";
import type { ToolContext, ToolDef } from "../core/registry.js";
import { TOOLS } from "../tools/index.js";
import { withPluginTools, type CompassPlugin } from "../core/plugins.js";
import { CLI_VERSION } from "../version.js";
import { registerPrompts } from "./prompts.js";

export interface McpDeps {
  fetchImpl?: typeof fetch;
  connection?: Connection | null;
  stderr?: (text: string) => void;
  plugins?: CompassPlugin[];
}

function text(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

export function createServer(deps: McpDeps = {}): McpServer {
  const server = new McpServer(
    { name: "compass", version: CLI_VERSION },
    {
      instructions:
        "Compass tools. Start with compass_status. Tools whose description mentions a plan return {executed:false, plan} until called with confirm=true; show the plan to the user and ask before confirming. Results marked sensitive (lure material, tokens, presigned URLs) must not be echoed into logs, PRs or chat unless the user asks.",
    },
  );
  const log = (line: string) => (deps.stderr || ((t: string) => process.stderr.write(t + "\n")))(line);

  const contextFor = async (): Promise<ToolContext> => {
    const connection = deps.connection !== undefined ? deps.connection : await resolveConnection();
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
      log,
    };
  };

  // Tools of a captain that is off in the workspace are hidden (compass_core.captains):
  // an agent is never handed a verb that would be refused. Checked once at start and
  // after every compass_captains call; an API without /api/captains hides nothing.
  const registered = new Map<ToolDef<any>, { enable(): void; disable(): void; enabled: boolean }>();
  const refreshCaptainTools = async () => {
    try {
      const ctx = await contextFor();
      const listing = await ctx.client().get("/api/captains");
      const inactive = new Set<string>((listing?.captains || []).filter((c: any) => c && c.active === false).map((c: any) => String(c.name)));
      for (const [tool, handle] of registered) {
        if (!tool.captain) continue;
        const wanted = !inactive.has(tool.captain);
        if (wanted && !handle.enabled) handle.enable();
        if (!wanted && handle.enabled) handle.disable();
      }
    } catch {
      // not configured, or an API without the Captains page: every tool stays listed
    }
  };

  const plugins = deps.plugins || [];
  for (const tool of withPluginTools(TOOLS, plugins, log)) {
    const handle = server.registerTool(
      tool.name,
      {
        title: tool.cli.command.replace(/\s+/g, " "),
        description: tool.sensitive ? `${tool.description}\n\nSensitive output: do not echo secrets or lure material.` : tool.description,
        inputSchema: tool.input,
        annotations: {
          readOnlyHint: tool.annotations?.readOnlyHint ?? false,
          destructiveHint: tool.annotations?.destructiveHint ?? false,
          idempotentHint: tool.annotations?.idempotentHint ?? false,
          openWorldHint: tool.annotations?.openWorldHint ?? false,
        },
      },
      async (args: Record<string, unknown>) => {
        try {
          const ctx = await contextFor();
          const parsed = z.object(tool.input).parse(args ?? {});
          const result = await tool.handler(parsed, ctx);
          if (tool.name === "compass_captains") void refreshCaptainTools();
          const isPlan = !!(result && typeof result === "object" && (result as any).executed === false && (result as any).plan);
          const body = isPlan ? `PLAN ONLY — nothing executed. Call again with confirm=true after the user agrees.\n${text(result)}` : text(result);
          return { content: [{ type: "text" as const, text: body }] };
        } catch (error: any) {
          const message = error instanceof CompassError ? `${error.code}: ${error.message}` : error instanceof z.ZodError ? `invalid arguments: ${error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; ")}` : error?.message || String(error);
          return { content: [{ type: "text" as const, text: message }], isError: true };
        }
      },
    );
    registered.set(tool, handle);
  }

  registerPrompts(server, plugins.map((p) => p.skillsDir).filter((d): d is string => !!d));
  (server as any).refreshCaptainTools = refreshCaptainTools;
  return server;
}

export async function runMcpServer(deps: McpDeps = {}): Promise<void> {
  const server = createServer(deps);
  const transport = new StdioServerTransport();
  await server.connect(transport);
  await refreshCaptainTools(server);
}

/** Hide the tools of captains that are off; call after connecting (and whenever the set may have changed). */
export async function refreshCaptainTools(server: McpServer): Promise<void> {
  const refresh = (server as any).refreshCaptainTools as (() => Promise<void>) | undefined;
  if (refresh) await refresh();
}
