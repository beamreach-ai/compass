import { describe, expect, it } from "vitest";
import { z } from "zod";
import { buildProgram } from "../src/cli/program.js";
import { defineTool, plan, type CompassPlugin } from "../src/plugin.js";
import { connection, fakeEngine } from "./helpers.js";

// A plugin defined here: generic CLI behaviour (rendering, plans, exit codes,
// validation, nested commands) is exercised through it, and so is the plugin path.
const rows = [
  { id: "b", severity: "low", owner: "acme" },
  { id: "a", severity: "critical", owner: "acme" },
];
const demoPlugin: CompassPlugin = {
  name: "demo-plugin",
  version: "1.0.0",
  tools: [
    defineTool({
      name: "compass_demo",
      description: "Demo rows: list them, or count them by severity.",
      input: { action: z.enum(["list", "count"]).default("list"), owner: z.string().optional(), region: z.string().optional() },
      scope: "read",
      annotations: { readOnlyHint: true },
      cli: { command: "demo", positionals: ["action"] },
      handler: async (args, ctx) => {
        const all = await ctx.client().get("/api/demo");
        const list = (all as any[]).filter((r) => !args.owner || r.owner === args.owner);
        return args.action === "count" ? { count: list.length, region: args.region ?? null } : list;
      },
    }),
    defineTool({
      name: "compass_demo_check",
      description: "Check the demo prerequisites in a region.",
      input: { region: z.string().optional(), aws_profile: z.string().optional() },
      scope: "read",
      annotations: { readOnlyHint: true },
      cli: { command: "demo check" },
      handler: async (args) => ({ region: args.region ?? null, aws_profile: args.aws_profile ?? null }),
    }),
    defineTool({
      name: "compass_demo_apply",
      description: "Apply a demo change. Returns a plan unless confirm=true.",
      input: { ids: z.array(z.string()), level: z.number().int().min(0).max(5).optional(), confirm: z.boolean().optional() },
      scope: "operate",
      confirm: true,
      cli: { command: "demo apply", positionals: ["ids"] },
      handler: async (args, ctx) => {
        if (!args.confirm) return plan(`Apply ${args.ids.length} change(s)`, args.ids.map((id) => `change ${id}`));
        return ctx.client().post("/api/demo/apply", { ids: args.ids });
      },
    }),
  ],
};

function run(argv: string[], routes: Record<string, unknown>, plugins: CompassPlugin[] = [demoPlugin]) {
  const engine = fakeEngine(routes);
  const stdout: string[] = [];
  const stderr: string[] = [];
  let exitCode: number | undefined;
  const program = buildProgram({
    fetchImpl: engine.fetchImpl,
    connection,
    plugins,
    stdout: (t) => stdout.push(t),
    stderr: (t) => stderr.push(t),
    exit: (code) => {
      exitCode = code;
    },
  });
  program.exitOverride();
  return program.parseAsync(["node", "compass", ...argv]).then(() => ({ stdout: stdout.join("\n"), stderr: stderr.join("\n"), exitCode, calls: engine.calls }));
}

describe("compass CLI", () => {
  it("runs a plugin tool with filters and --json", async () => {
    const r = await run(["demo", "list", "--owner", "acme", "--json"], { "GET /api/demo": rows });
    expect(r.exitCode).toBeUndefined();
    expect(JSON.parse(r.stdout).map((x: any) => x.id)).toEqual(["b", "a"]);
  });

  it("renders a table for humans", async () => {
    const r = await run(["demo", "list"], { "GET /api/demo": rows });
    expect(r.stdout).toMatch(/id\s+severity\s+owner/);
  });

  it("a confirm-gated tool prints the plan and exits 2; with --yes it acts", async () => {
    const planned = await run(["demo", "apply", "x1", "x2"], {});
    expect(planned.exitCode).toBe(2);
    expect(planned.stdout).toContain("Apply 2 change(s)");
    expect(planned.calls.find((c) => c.path === "/api/demo/apply")).toBeUndefined();
    const done = await run(["demo", "apply", "x1", "--yes", "--json"], { "POST /api/demo/apply": { applied: 1 } });
    expect(done.calls.find((c) => c.path === "/api/demo/apply")?.body).toEqual({ ids: ["x1"] });
  });

  it("maps typed errors to exit codes and json error objects", async () => {
    const r = await run(["demo", "list", "--json"], { "GET /api/demo": { status: 402, body: { detail: "limit reached" } } });
    expect(r.exitCode).toBe(3);
    expect(JSON.parse(r.stdout).error).toBe("limit_reached");
    const human = await run(["demo", "list"], { "GET /api/demo": { status: 402, body: { detail: "limit reached" } } });
    expect(human.stderr).toContain("limit_reached");
  });

  it("validates arguments against the schema", async () => {
    const r = await run(["demo", "apply", "x1", "--level", "9"], {});
    expect(r.exitCode).toBe(1);
    expect(r.stderr).toContain("Invalid arguments");
  });

  it("without the plugin, its commands do not exist and `plugins` says none are loaded", async () => {
    const r = await run(["plugins"], {}, []);
    expect(r.stdout).toContain("No plugins loaded");
    await expect(run(["demo", "list"], {}, [])).rejects.toThrow();
    const listed = await run(["plugins"], {});
    expect(listed.stdout).toContain("demo-plugin 1.0.0: 3 tools");
  });

  it("login --from-aws needs a plugin that provides it", async () => {
    const r = await run(["login", "--from-aws"], {}, []);
    expect(r.exitCode).toBe(1);
    expect(r.stderr).toContain("@beamreach/compass-enterprise");
  });

  it("a plugin cannot replace a built-in tool", async () => {
    const clash: CompassPlugin = { name: "clash", tools: [{ ...demoPlugin.tools![0], name: "compass_status" }] };
    const r = await run(["status", "--json"], { "GET /api/status": { api_version: "x" } }, [clash]);
    expect(r.stderr).toContain("clash tool compass_status skipped");
    expect(JSON.parse(r.stdout).api_version).toBe("x");
  });

  it("tokens create posts name and scope and shows the token once", async () => {
    const r = await run(["tokens", "create", "--name", "ci", "--scope", "read", "--json"], {
      "POST /api/auth/tokens": (req: any) => ({ token_id: "tok_1", token: "cmp_new", ...req.body }),
    });
    expect(JSON.parse(r.stdout)).toMatchObject({ token: "cmp_new", name: "ci", scope: "read" });
    expect(r.stderr).toContain("Store this token now");
  });

  it("status renders the brief", async () => {
    const r = await run(["status"], {
      "GET /api/status": {
        api_version: "2026.10.1",
        compass_version: "v1.2.0",
        workspace: { workspace_id: "ws-test" },
        app: { mode: "production", read_only: false },
        target: { aws_connected: true, aws: { mode: "roles", account_id: "123456789012", region: "eu-central-1", verified_at: "2026-10-20T10:00:00Z" }, github_connected: false },
        access: { actions_armed: 2, actions_total: 3, audited_at: "3h ago" },
        infra_scan: { scanned: true },
        findings: { by_domain: { security: { critical: 1, high: 2, medium: 0, low: 5 }, finops: { low: 3 } }, estimated_monthly_savings_usd: 120 },
        jobs: { active_count: 1, recent_failures: [] },
        prs: { opened: 2, pending: 1 },
        honeypots: { total: 3, by_status: { armed: 2, pending_pr: 1 }, active_alerts: 0 },
        billing: { plan: "free" },
        errors: {},
      },
    });
    expect(r.stdout).toContain("AWS: account 123456789012 (eu-central-1) · read ✓ · 2 of 3 actions armed · audited 3h ago");
    expect(r.stdout).toContain("Security findings: 1 critical, 2 high");
    expect(r.stdout).toContain("Honeypots: 3 (2 armed, 1 pending_pr), 0 active alerts");
  });

  it("honeypots plant --top-n builds the autopilot batch from suggestions", async () => {
    const r = await run(["honeypots", "plant", "--top-n", "2", "--yes", "--json"], {
      "GET /api/notifications/endpoints": [{ endpoint_id: "e1" }],
      "GET /api/honeypots/suggestions": [
        { resource_id: "r3", recommended_class: "s3_object", priority: 3 },
        { resource_id: "r1", recommended_class: "aws_credential", priority: 1, region: "eu-west-1" },
        { resource_id: "r2", recommended_class: "db_credential", priority: 2 },
      ],
      "POST /api/honeypots/autopilot": (req: any) => ({ planted: req.body.suggestions.length }),
    });
    const call = r.calls.find((c) => c.path === "/api/honeypots/autopilot")!;
    expect((call.body as any).suggestions.map((s: any) => s.resource_id)).toEqual(["r1", "r2"]);
    expect((call.body as any).notification_endpoint_ids).toEqual(["e1"]);
    expect(JSON.parse(r.stdout)).toMatchObject({ executed: true, planted: 2 });
  });

});

describe("option shadowing between a leaf and its subcommands", () => {
  it("passes --region/--aws-profile to `demo check`, not to `demo`", async () => {
    const r = await run(["demo", "check", "--region", "eu-west-3", "--aws-profile", "p1", "--json"], {});
    expect(JSON.parse(r.stdout)).toEqual({ region: "eu-west-3", aws_profile: "p1" });
  });

  it("still accepts global flags after the subcommand", async () => {
    const r = await run(["demo", "list", "--owner", "acme", "--json"], { "GET /api/demo": rows });
    expect(JSON.parse(r.stdout).length).toBe(2);
  });
});

describe("global flags on a node that is both a group and a leaf", () => {
  it("accepts --json and options after `demo` even though `demo check` was registered too", async () => {
    const r = await run(["demo", "count", "--region", "x", "--json"], { "GET /api/demo": rows });
    expect(r.stderr + r.stdout).not.toMatch(/unknown option/);
    expect(JSON.parse(r.stdout)).toEqual({ count: 2, region: "x" });
  });
});
