import { describe, expect, it } from "vitest";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { buildProgram } from "../src/cli/program.js";
import { createServer, refreshCaptainTools } from "../src/mcp/server.js";
import { TOOLS } from "../src/tools/index.js";
import { connection, fakeEngine } from "./helpers.js";

const listing = {
  core_version: "0.1.0",
  licence: { plan: "free", captains: [], tiers: [] },
  captains: [
    { name: "core", label: "Compass", tier: "open", core: true, active: true, enabled: true, reason: null, blocked_by: [] },
    { name: "mapper", label: "Infra map", tier: "open", core: false, active: true, enabled: true, reason: null, blocked_by: [] },
    { name: "iac", label: "Code-as-cloud", tier: "open", core: false, active: false, enabled: false, reason: "disabled_by_admin", blocked_by: [] },
    { name: "honeypot", label: "Decoys", tier: "open", core: false, active: true, enabled: true, reason: null, blocked_by: [] },
    { name: "prs", label: "Pull requests", tier: "open", core: false, active: true, enabled: true, reason: null, blocked_by: [] },
  ],
};

function run(argv: string[], routes: Record<string, unknown>) {
  const engine = fakeEngine(routes);
  const stdout: string[] = [];
  const stderr: string[] = [];
  let exitCode: number | undefined;
  const program = buildProgram({
    fetchImpl: engine.fetchImpl,
    connection,
    stdout: (t) => stdout.push(t),
    stderr: (t) => stderr.push(t),
    exit: (code) => {
      exitCode = code;
    },
  });
  program.exitOverride();
  return program.parseAsync(["node", "compass", ...argv]).then(() => ({ stdout: stdout.join("\n"), stderr: stderr.join("\n"), exitCode, calls: engine.calls }));
}

describe("compass captains", () => {
  it("lists captains with their state", async () => {
    const r = await run(["captains", "list"], { "GET /api/captains": listing });
    expect(r.exitCode).toBeUndefined();
    expect(r.stdout).toMatch(/iac\s+open\s+off\s+disabled_by_admin/);
    expect(r.stdout).not.toMatch(/^core/m);
  });

  it("enables with dependencies and sets config", async () => {
    const r = await run(["captains", "enable", "iac", "--with-dependencies"], {
      "POST /api/captains/iac/enable": { changed: ["mapper", "iac"], captain: { name: "iac", label: "Code-as-cloud", active: true } },
    });
    expect(r.calls[0].body).toEqual({ with_dependencies: true });
    expect(r.stdout).toContain("Code-as-cloud: on; also changed: mapper");
    const c = await run(["captains", "config_set", "prs", "--config", '{"watcher":{"risk_max":1}}', "--json"], {
      "PUT /api/captains/prs/config": { config: { watcher: { risk_max: 1 } } },
    });
    expect(c.calls[0].method).toBe("PUT");
    expect(c.calls[0].body).toEqual({ watcher: { risk_max: 1 } });
  });

  it("lists the catalog with the line to install", async () => {
    const r = await run(["captains", "catalog", "--query", "hello"], {
      "GET /api/captains/catalog": {
        enabled: true, source: "bundled", error: null, core_version: "0.3.0",
        entries: [
          { name: "hello", tier: "open", version: "0.1.0", status: "install", licensed: true, env_line: "COMPASS_EXTENSIONS=compass-captain-hello==0.1.0" },
          { name: "engine", tier: "cloud", version: "0.2.0", status: "vendor", licensed: true, env_line: null },
        ],
      },
    });
    expect(r.calls[0].query).toEqual({ q: "hello" });
    expect(r.stdout).toMatch(/hello\s+open\s+0\.1\.0\s+install/);
    expect(r.stdout).toContain("COMPASS_EXTENSIONS=compass-captain-hello==0.1.0");
    expect(r.stdout).toMatch(/engine\s+cloud\s+0\.2\.0\s+vendor/);
  });

  it("plans an install, then installs on --yes", async () => {
    const routes = {
      "GET /api/captains/catalog": { entries: [{ name: "hello", label: "Hello", version: "0.1.0", tier: "open", status: "install", licensed: true, pip: "hello==0.1.0", source: "https://src" }] },
      "POST /api/captains/install": { captain: "hello", restart_required: true },
    };
    const planned = await run(["extensions", "install", "hello", "--json"], routes);
    expect(JSON.parse(planned.stdout).plan.summary).toBe("Install Hello 0.1.0 (open) from the catalog");
    expect(planned.calls.find((c) => c.path === "/api/captains/install")).toBeUndefined();
    const done = await run(["extensions", "install", "hello", "--yes", "--json"], routes);
    expect(done.calls.find((c) => c.path === "/api/captains/install")?.body).toEqual({ name: "hello" });
    expect(JSON.parse(done.stdout).restart_required).toBe(true);
  });

  it("explains a refusal for a captain that is off", async () => {
    const r = await run(["prs", "sync"], {
      "POST /api/prs/sync": { status: 403, body: { detail: "Pull requests is turned off for this workspace.", code: "captain_disabled", captain: "prs" } },
    });
    expect(r.exitCode).toBe(7);
    expect(r.stderr).toContain("captain_disabled");
    expect(r.stderr).toContain("compass captains enable prs");
  });

  it("hides the tools of a captain that is off from the MCP listing", async () => {
    const engine = fakeEngine({ "GET /api/captains": listing });
    const server = createServer({ fetchImpl: engine.fetchImpl, connection, stderr: () => undefined });
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    await server.connect(serverTransport);
    const client = new Client({ name: "test", version: "0" });
    await client.connect(clientTransport);
    await refreshCaptainTools(server);
    const { tools } = await client.listTools();
    const names = tools.map((t) => t.name);
    const iacTools = TOOLS.filter((t) => t.captain === "iac").map((t) => t.name);
    expect(iacTools.length).toBeGreaterThan(0);
    for (const name of iacTools) expect(names).not.toContain(name);
    expect(names).toContain("compass_honeypots");
    expect(names).toContain("compass_captains");
  });
});

describe("status", () => {
  it("says when a Compass release is out and when the compose file asks for another version", async () => {
    const base = { compass_version: "0.5.2", api_version: "2026.10.1", workspace: { workspace_id: "local" }, app: { mode: "production" }, errors: {} };
    const out = await run(["status"], {
      "GET /api/status": { ...base, update: { available: true, latest: "0.6.0", running: "0.5.2", apply: "manual", pinned: "0.5.1", pinned_differs: true } },
    });
    expect(out.stdout).toContain("Update: Compass 0.6.0 is available (this is 0.5.2); Settings → Updates shows the two commands");
    expect(out.stdout).toContain("the compose file asks for 0.5.1 but 0.5.2 is running");
    const quiet = await run(["status"], { "GET /api/status": { ...base, update: { available: false, running: "0.5.2" } } });
    expect(quiet.stdout).not.toContain("Update:");
  });
});
