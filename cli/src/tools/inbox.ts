import { z } from "zod";
import { defineTool, plan } from "../core/registry.js";

const VERBS = ["approve", "fix", "merge", "later"] as const;

function since(ms: number): string {
  if (!ms) return "";
  const s = Math.floor((Date.now() - ms) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export const inboxTool = defineTool({
  name: "compass_inbox",
  description:
    "Everything that needs a person, as one list derived from state, newest first: parked decoy responses, grants to accept, decoys waiting for a merge or an arm, pull requests with a revision to approve or that the watcher gave up on, failed jobs, access findings, an available update, an incomplete setup, patch findings waiting for approval. Each item carries its actions (key, label, the route, and a confirm sentence where one is needed) and a detail block. Read this before acting; act with compass_inbox_act.",
  input: {
    tab: z.string().optional().describe("Only this tab's items (honeypots, prs, queue, settings, patches, …)"),
    verb: z.enum(VERBS).optional().describe("Only items asking this: approve (a decision), fix (a repair), merge (on GitHub), later (a chore)"),
    include_snoozed: z.boolean().optional(),
  },
  scope: "read",
  annotations: { readOnlyHint: true },
  cli: { command: "inbox", summary: "[--tab …] [--verb …] [--include-snoozed]" },
  handler: async (args, ctx) => ctx.client().get("/api/inbox", { tab: args.tab, verb: args.verb, include_snoozed: args.include_snoozed ? "true" : undefined }),
  render: (result) => {
    const r = result as any;
    if (!r || !Array.isArray(r.items)) return JSON.stringify(result, null, 2);
    const c = r.counts || {};
    const byVerb = VERBS.map((v) => (c.by_verb?.[v] ? `${c.by_verb[v]} to ${v === "later" ? "do later" : v}` : null)).filter(Boolean).join(", ");
    const lines = [c.total ? `Needs you: ${c.total}${c.urgent ? ` (${c.urgent} urgent)` : ""}${byVerb ? `: ${byVerb}` : ""}` : "Needs you: nothing"];
    for (const item of r.items) {
      const primary = (item.actions || []).find((a: any) => a.method) || item.actions?.[0];
      lines.push(`  ${since(item.since).padEnd(8)} ${item.severity.padEnd(6)} ${item.verb.padEnd(7)} ${item.title}${item.snooze ? "  (snoozed)" : ""}`);
      lines.push(`           ${item.id}${primary ? `  → compass inbox act ${item.id} ${primary.key}` : ""}`);
    }
    return lines.join("\n");
  },
});

export const inboxActTool = defineTool({
  name: "compass_inbox_act",
  description:
    "Perform one of an inbox item's actions by key (run a parked response, accept a grant, retry a job, approve a revision, arm a decoy, approve a patch pull request, acknowledge, …): the same route the tab calls. Returns the item's confirm sentence as a plan unless confirm=true; show that sentence to the person and act only on their yes. Never act on an `approve` item without it.",
  input: {
    id: z.string().describe("The item id (compass_inbox)"),
    action: z.string().optional().describe("The action key; default: the item's primary action"),
    confirm: z.boolean().optional().describe("Perform it instead of returning the plan"),
  },
  scope: "operate",
  confirm: true,
  annotations: { openWorldHint: true },
  cli: { command: "inbox act", positionals: ["id", "action"], summary: "<id> [action] [--yes]" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    const feed = (await client.get("/api/inbox", { include_snoozed: "true" })) as any;
    const item = (feed?.items || []).find((i: any) => i.id === args.id);
    if (!item) throw new Error(`no inbox item ${args.id} (it may have left: compass inbox)`);
    const actions: any[] = item.actions || [];
    const action = args.action ? actions.find((a) => a.key === args.action) : actions.find((a) => a.method) || actions[0];
    if (!action) throw new Error(`item ${args.id} has no action ${args.action || ""} (it has: ${actions.map((a) => a.key).join(", ")})`);
    if (!action.method) return { href: action.href, label: action.label, note: "this action is a link; open it in the dashboard" };
    if (!args.confirm) {
      return plan(`${action.label}: ${item.title}`, [action.confirm || `${action.method} ${action.path}`, `item ${item.id} (${item.kind}, ${item.severity})`], {
        item: { id: item.id, kind: item.kind, title: item.title, summary: item.summary, detail: item.detail },
        action,
      });
    }
    const result = await client.request("POST" === action.method ? "POST" : action.method, action.path, { body: action.body ?? {} });
    ctx.log(`${action.label}: done`);
    return result;
  },
});

export const inboxSnoozeTool = defineTool({
  name: "compass_inbox_snooze",
  description: "Snooze an item that has a natural end (a failed job until it fails again, an update until the next release, no model on purpose), or un-snooze it.",
  input: { id: z.string(), unsnooze: z.boolean().optional() },
  scope: "operate",
  annotations: { idempotentHint: true },
  cli: { command: "inbox snooze", positionals: ["id"], summary: "<id> [--unsnooze]" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    const path = `/api/inbox/${args.id}/snooze`;
    return args.unsnooze ? client.request("DELETE", path) : client.post(path, {});
  },
});
