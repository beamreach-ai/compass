import { describe, expect, it } from "vitest";
import { CompassClient } from "../src/core/client.js";
import { AuthError, InfraScanPendingError, LimitReachedError, ScopeError, ApiError } from "../src/core/errors.js";
import { connection, fakeEngine } from "./helpers.js";

describe("CompassClient", () => {
  it("sends bearer token and workspace header, serialises params", async () => {
    const engine = fakeEngine({ "GET /api/findings": [{ id: "f1" }] });
    const client = new CompassClient(connection, engine.fetchImpl);
    const result = await client.get("/api/findings", { limit: 10, domain: undefined, flag: false });
    expect(result).toEqual([{ id: "f1" }]);
    const call = engine.calls[0];
    expect(call.headers.authorization).toBe("Bearer cmp_test-token");
    expect(call.headers["x-workspace-id"]).toBe("ws-test");
    expect(call.query).toEqual({ limit: "10", flag: "false" });
  });

  it("maps engine errors to typed errors", async () => {
    const engine = fakeEngine({
      "GET /a": { status: 401, body: { detail: "nope" } },
      "GET /b": { status: 403, body: { detail: "scope", required_scope: "admin", token_scope: "read" } },
      "GET /c": { status: 402, body: { detail: "Fix limit reached" } },
      "GET /d": { status: 409, body: { detail: "Complete the infra scan before Beamreach can run fixes." } },
      "GET /e": { status: 500, body: { detail: "boom" } },
    });
    const client = new CompassClient(connection, engine.fetchImpl);
    await expect(client.get("/a")).rejects.toBeInstanceOf(AuthError);
    await expect(client.get("/b")).rejects.toBeInstanceOf(ScopeError);
    await expect(client.get("/c")).rejects.toBeInstanceOf(LimitReachedError);
    await expect(client.get("/d")).rejects.toBeInstanceOf(InfraScanPendingError);
    const err = await client.get("/e").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(500);
    expect(err.message).toContain("boom");
  });

  it("reports unreachable hosts clearly", async () => {
    const client = new CompassClient({ url: "http://127.0.0.1:1" }, async () => {
      throw new Error("ECONNREFUSED");
    });
    await expect(client.get("/health")).rejects.toThrow(/Could not reach Compass/);
  });
});
