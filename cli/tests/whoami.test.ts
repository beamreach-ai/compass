import { describe, expect, it } from "vitest";
import { buildProgram } from "../src/cli/program.js";
import { connection, fakeEngine } from "./helpers.js";

function run(argv: string[], routes: Record<string, unknown>) {
  const engine = fakeEngine(routes);
  const stdout: string[] = [];
  const program = buildProgram({ fetchImpl: engine.fetchImpl, connection, plugins: [], stdout: (t) => stdout.push(t), stderr: () => {}, exit: () => {} });
  program.exitOverride();
  return program.parseAsync(["node", "compass", ...argv]).then(() => ({ out: stdout.join("\n"), calls: engine.calls }));
}

const principal = { id: "ilia", kind: "user", workspace_id: "local", org_id: null, email: "ilia@example.com", name: "Ilia", roles: ["read"], teams: ["sec"], source: "header" };

describe("compass whoami", () => {
  it("prints the principal the engine resolved", async () => {
    const { out, calls } = await run(["whoami", "--json"], { "GET /api/auth/me": { authenticated: true, auth_enabled: false, principal } });
    expect(JSON.parse(out)).toEqual(principal);
    expect(calls.map((c) => c.path)).toEqual(["/api/auth/me"]);
  });

  it("stands in for an engine without the principal seam", async () => {
    const token = { authenticated: true, auth_enabled: true, method: "api_token", token_id: "tok_1", kind: "personal", scope: "operate", workspace_id: "beamreach", org_id: null, user_id: "ops", name: "ops@laptop" };
    const { out } = await run(["whoami", "--json"], { "GET /api/auth/me": token });
    expect(JSON.parse(out)).toEqual({ id: "ops", kind: "token", workspace_id: "beamreach", org_id: null, email: null, name: "ops@laptop", roles: ["operate"], teams: [], source: "api_token" });
  });
});
