import { defineTool } from "../core/registry.js";

/** Who this connection is to the engine: GET /api/auth/me's `principal`. Engines before the
 *  principal seam answer without one; the token's public form or the cookie's username stands in. */
export const whoamiTool = defineTool({
  name: "compass_whoami",
  description:
    "Who the current connection is to Compass: id, kind (user or token), roles, teams, source (api_token, session, header, anonymous) and the workspace. Use it to know what the caller may do before acting, or when a call returns 403.",
  input: {},
  scope: "read",
  annotations: { readOnlyHint: true },
  cli: { command: "whoami", summary: "Who this connection is: id, roles, teams, source, workspace" },
  handler: async (_args, ctx) => {
    const me = await ctx.client().get("/api/auth/me");
    if (me?.principal) return me.principal;
    if (me?.method === "api_token") {
      return { id: me.user_id || me.token_id, kind: "token", workspace_id: me.workspace_id, org_id: me.org_id ?? null, email: null, name: me.name ?? null, roles: [me.scope], teams: [], source: "api_token" };
    }
    return { id: me?.username || "anonymous", kind: "user", workspace_id: null, org_id: null, email: null, name: null, roles: ["admin"], teams: [], source: me?.username ? "session" : "anonymous" };
  },
});
