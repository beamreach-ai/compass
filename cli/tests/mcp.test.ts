import { describe, expect, it } from "vitest";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { createServer } from "../src/mcp/server.js";
import { TOOLS } from "../src/tools/index.js";
import { connection, fakeEngine } from "./helpers.js";

import { z } from "zod";
import { defineTool, plan, type CompassPlugin } from "../src/plugin.js";

const plugin: CompassPlugin = {
  name: "demo",
  tools: [
    defineTool({
      name: "compass_demo_apply",
      description: "Apply a demo change. Returns a plan unless confirm=true.",
      input: { ids: z.array(z.string()), confirm: z.boolean().optional() },
      scope: "operate",
      confirm: true,
      cli: { command: "demo apply" },
      handler: async (args, ctx) => (args.confirm ? ctx.client().post("/api/demo/apply", { ids: args.ids }) : plan("Apply", args.ids)),
    }),
  ],
};

async function connect(routes: Record<string, unknown>, plugins: CompassPlugin[] = []) {
  const engine = fakeEngine(routes);
  const server = createServer({ fetchImpl: engine.fetchImpl, connection, stderr: () => undefined, plugins });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  const client = new Client({ name: "test", version: "0" });
  await client.connect(clientTransport);
  return { client, engine };
}

describe("MCP server", () => {
  it("exposes every registry tool with annotations and JSON schemas", async () => {
    const { client } = await connect({});
    const { tools } = await client.listTools();
    expect(tools.map((t) => t.name).sort()).toEqual(TOOLS.map((t) => t.name).sort());
    const plant = tools.find((t) => t.name === "compass_honeypots_plant")!;
    expect((plant.inputSchema as any).properties.confirm).toBeDefined();
    const status = tools.find((t) => t.name === "compass_status")!;
    expect(status.annotations?.readOnlyHint).toBe(true);
  });

  it("calls a tool and returns JSON text", async () => {
    const { client } = await connect({ "GET /api/status": { api_version: "2026.10.1", findings: { total: 2 } } });
    const result = await client.callTool({ name: "compass_status", arguments: {} });
    const text = (result.content as any[])[0].text;
    expect(JSON.parse(text).findings.total).toBe(2);
  });

  it("returns a plan for confirm-gated tools and errors as isError", async () => {
    const { client, engine } = await connect({
      "GET /api/auth/tokens": { status: 403, body: { detail: "needs admin", required_scope: "admin", token_scope: "operate" } },
    }, [plugin]);
    const { tools } = await client.listTools();
    expect(tools.map((t) => t.name)).toContain("compass_demo_apply");
    const planned = await client.callTool({ name: "compass_demo_apply", arguments: { ids: ["f1"] } });
    expect((planned.content as any[])[0].text).toMatch(/^PLAN ONLY/);
    expect(engine.calls.find((c) => c.path === "/api/demo/apply")).toBeUndefined();

    const denied = await client.callTool({ name: "compass_tokens", arguments: { action: "list" } });
    expect(denied.isError).toBe(true);
    expect((denied.content as any[])[0].text).toContain("scope");
  });
});
