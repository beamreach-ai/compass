import { z } from "zod";
import { defineTool, plan } from "../core/registry.js";

const PR_ID = /^([\w.-]+\/[\w.-]+)#(\d+)$/;

function parsePrId(id: string | undefined): { path: string; repo: string; number: number } {
  const match = PR_ID.exec((id || "").trim());
  if (!match) throw new Error("pr is required as owner/repo#number");
  return { path: `/api/prs/${match[1]}/${match[2]}`, repo: match[1], number: Number(match[2]) };
}

export const prsTool = defineTool({
  name: "compass_prs",
  description:
    "Pull requests Compass opened: security and FinOps fixes, Terraform imports, drift fixes, honeypot plants. list (filter by captain, state, needs_human); get <owner/repo#n> for the intent, revisions and watcher state; sync [pr] to refresh from GitHub now (all open PRs without a pr); address <pr> to revise it from the open review comments or a message (plan unless confirm); approve/dismiss a pending revision; pause/resume the watcher on one PR.",
  input: {
    action: z.enum(["list", "get", "sync", "address", "approve", "dismiss", "pause", "resume"]).default("list"),
    pr: z.string().optional().describe("owner/repo#number (all actions except list)"),
    captain: z.enum(["iac", "engine", "finops_engine", "honeypot"]).optional(),
    state: z.enum(["open", "merged", "closed"]).optional().describe("list: tracked PRs in this state"),
    needs_human: z.boolean().optional().describe("list: only PRs waiting for a person"),
    opened_only: z.boolean().optional().describe("list: drop jobs that have not produced a PR yet"),
    message: z.string().optional().describe("address: what should change; without it the open review comments are used"),
    confirm: z.boolean().optional().describe("address: run the revision instead of returning the plan"),
    limit: z.number().int().min(1).max(500).optional(),
  },
  scope: "operate",
  captain: "prs",
  confirm: true,
  annotations: { openWorldHint: true },
  cli: { command: "prs", positionals: ["action", "pr"], summary: "list | get <pr> | sync [pr] | address <pr> [--message] [--yes] | approve | dismiss | pause | resume" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    switch (args.action) {
      case "list": {
        const prs = (await client.get("/api/prs", {
          limit: args.limit ?? 200,
          captain: args.captain,
          state: args.state,
          needs_human: args.needs_human ? "true" : undefined,
        })) as any[];
        return prs.filter((p) => !args.opened_only || p.pr_url);
      }
      case "get":
        return client.get(parsePrId(args.pr).path);
      case "sync":
        return args.pr ? client.post(`${parsePrId(args.pr).path}/sync`, {}) : client.post("/api/prs/sync", {});
      case "address": {
        const { path, repo, number } = parsePrId(args.pr);
        const preview = (await client.post(`${path}/address`, { message: args.message, confirm: false })) as any;
        const p = preview?.plan || {};
        if (!args.confirm) {
          return plan(
            `Revise ${repo}#${number}`,
            [
              args.message ? `Request: ${args.message}` : "Address the open review comments from collaborators",
              `Mode ${p.mode}, risk ${p.risk?.risk ?? "?"} vs max ${p.risk_max}, revisions ${p.revisions_so_far}/${p.max_revisions}`,
              `Policy decision: ${p.decision}${p.reason ? ` (${p.reason})` : ""}`,
            ],
            { plan: p },
            p.decision === "blocked" ? [`Blocked: ${p.reason}`] : undefined,
          );
        }
        return { executed: true, ...(await client.post(`${path}/address`, { message: args.message, confirm: true })) };
      }
      case "approve":
        return client.post(`${parsePrId(args.pr).path}/revision/approve`, {});
      case "dismiss":
        return client.post(`${parsePrId(args.pr).path}/revision/dismiss`, {});
      case "pause":
        return client.post(`${parsePrId(args.pr).path}/pause`, {});
      case "resume":
        return client.post(`${parsePrId(args.pr).path}/resume`, {});
    }
  },
});

export const prsWatcherTool = defineTool({
  name: "compass_prs_watcher",
  description:
    "PR watcher policy: mode (off | approve | autopilot), the highest revision risk pushed without a person (risk_max), the revision cap per PR (max_revisions), whether Compass replies to reviewers, and the auto-merge block. get, or set any subset.",
  input: {
    action: z.enum(["get", "set"]),
    mode: z.enum(["off", "approve", "autopilot"]).optional(),
    risk_max: z.number().int().min(0).max(5).optional(),
    max_revisions: z.number().int().min(0).max(10).optional(),
    reply: z.boolean().optional(),
    auto_merge_enabled: z.boolean().optional(),
    auto_merge_risk_max: z.number().int().min(0).max(5).optional(),
    auto_merge_repos: z.array(z.string()).optional().describe("repos (owner/name) where auto-merge may be enabled"),
  },
  scope: "admin",
  captain: "prs",
  annotations: { idempotentHint: true },
  cli: { command: "prs watcher", positionals: ["action"], summary: "get | set --mode … --risk-max … --max-revisions …" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    if (args.action === "get") return client.get("/api/prs/watcher");
    const body: Record<string, unknown> = {};
    for (const key of ["mode", "risk_max", "max_revisions", "reply"] as const) if (args[key] !== undefined) body[key] = args[key];
    const autoMerge: Record<string, unknown> = {};
    if (args.auto_merge_enabled !== undefined) autoMerge.enabled = args.auto_merge_enabled;
    if (args.auto_merge_risk_max !== undefined) autoMerge.risk_max = args.auto_merge_risk_max;
    if (args.auto_merge_repos !== undefined) autoMerge.repos = args.auto_merge_repos;
    if (Object.keys(autoMerge).length) body.auto_merge = autoMerge;
    if (!Object.keys(body).length) throw new Error("nothing to set");
    return client.put("/api/prs/watcher", body);
  },
});
