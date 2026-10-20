import { z } from "zod";
import { defineTool } from "../core/registry.js";

const accessInput = {
  action: z.enum(["list", "accept", "decline", "remove", "verify", "audit", "pr"]).describe(
    "list: the roles and every grant with its state; accept/decline/remove <grant_id>: a person's decision (accept and remove open the pull request that grants or revokes exactly that action); verify: read the act role back from IAM and arm what it holds; audit: the self-audit of both roles; pr: open or refresh the access pull request",
  ),
  grant_id: z.string().optional().describe("The grant (accept, decline, remove); `list` shows the ids"),
  mode: z.enum(["approve", "autopilot"]).optional().describe("accept: the mode the gate reads"),
};

export const accessTool = defineTool({
  name: "compass_access",
  description:
    "What Compass may do in the account: every write is one grant with a lifecycle (proposed → pr_open → merged → applied → verified → armed; removal disarms at once and opens the revoke pull request). list and verify read; accept, decline and remove are a person's decisions.",
  input: accessInput,
  scope: "admin",
  annotations: { readOnlyHint: false, idempotentHint: true },
  cli: { command: "access", positionals: ["action", "grant_id"], summary: "list | accept | decline | remove <grant> | verify | audit | pr" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    if (args.action === "list") return client.get("/api/access");
    if (args.action === "verify") return client.post("/api/access/verify", {});
    if (args.action === "audit") return client.post("/api/access/audit", {});
    if (args.action === "pr") return client.post("/api/access/pr", {});
    if (!args.grant_id) throw new Error(`${args.action} needs a grant id (compass access list)`);
    const body = args.action === "accept" && args.mode ? { mode: args.mode } : {};
    const result = await client.post(`/api/access/grants/${args.grant_id}/${args.action}`, body);
    if (args.action === "accept") ctx.log("Accepted: the pull request that grants it is queued; the action arms once it is merged, applied and verified.");
    if (args.action === "remove") ctx.log("Removed: disarmed now; the pull request that takes the permission away is queued.");
    return result;
  },
  render: (result) => {
    const r = result as any;
    if (!r || !Array.isArray(r.grants)) return JSON.stringify(result, null, 2);
    const lines = [`Roles: read ${r.roles?.read || "none"} · act ${r.roles?.act || "none"}`, `${r.summary?.armed ?? 0} of ${r.summary?.total ?? 0} actions armed`];
    for (const g of r.grants) lines.push(`  ${g.grant_id}  ${g.captain}.${g.action_type}  ${g.mode}  ${g.grant_status}${g.grant_pr ? `  ${g.grant_pr}` : ""}`);
    if (Array.isArray(r.findings) && r.findings.length) lines.push(`Findings: ${r.findings.length} (compass access audit)`);
    return lines.join("\n");
  },
});
