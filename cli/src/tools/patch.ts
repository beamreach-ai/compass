import { z } from "zod";
import { defineTool } from "../core/registry.js";

const SEVERITY_ORDER: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 };

function findingLine(f: any): string {
  const score = [f.cvss != null ? `CVSS ${f.cvss}` : null, f.kev ? "KEV" : null, f.epss != null ? `EPSS ${Math.round(Number(f.epss) * 100)}%` : null].filter(Boolean).join(" ");
  const where = (f.workloads || []).map((w: any) => `${w.name}${w.exposed ? " (exposed)" : ""}`).join(", ") || (f.repo ? `${f.repo}/${f.path || ""}` : f.node_name);
  const fix = f.fixed_version ? `→ ${f.fixed_version}` : "no fix yet";
  const state = f.state === "pr_open" && f.pr?.url ? `pull request ${f.pr.url}` : f.state === "open" ? (f.verdict === "pr" ? "pull request next tick" : "waits for you") : f.state;
  return `  ${f.key}  ${(f.severity || "?").padEnd(8)} ${f.advisory_id}  ${f.package} ${f.version || "(unpinned)"} ${fix}  ${where}  ${state}${score ? `  [${score}]` : ""}`;
}

export const patchFindingsTool = defineTool({
  name: "compass_patch_findings",
  description:
    "The CVEs on the map and where they land: every finding with its package and the version that fixes it, the workloads that run it and whether they face the internet, the repository and file the fix goes to, the verdict (a pull request without a click, or an inbox item) and the state (open, approved, pr_open, merged, deployed, ignored, closed). Filter by exposed, exploited (KEV), state or repository. The summary counts what is open, exposed, exploited and waiting for merge; `feed` says when each source last ran and how many advisories are stored.",
  input: {
    exposed: z.boolean().optional().describe("Only findings on workloads that face the internet"),
    exploited: z.boolean().optional().describe("Only findings in CISA's known-exploited catalogue"),
    state: z.enum(["open", "approved", "pr_open", "merged", "deployed", "ignored", "closed"]).optional().describe("Default: every open state"),
    repo: z.string().optional().describe("Only findings whose fix goes to this repository (owner/name)"),
    key: z.string().optional().describe("One finding's key: its row and the advisory behind it"),
  },
  scope: "read",
  captain: "patch",
  annotations: { readOnlyHint: true },
  cli: { command: "patch findings", positionals: ["key"], summary: "[key] [--exposed] [--exploited] [--state] [--repo]" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    if (args.key) return client.get(`/api/patches/findings/${encodeURIComponent(args.key)}`);
    return client.get("/api/patches", {
      exposed: args.exposed ? "true" : undefined,
      exploited: args.exploited ? "true" : undefined,
      state: args.state,
      repo: args.repo,
    });
  },
  render: (result) => {
    const r = result as any;
    if (r?.finding) {
      const f = r.finding;
      const lines = [findingLine(f).trim(), `  why: ${(f.verdict_reasons || []).join("; ")}`, `  evidence: ${f.evidence || ""}${f.relation ? ` (${f.relation})` : ""}`];
      if (f.note) lines.push(`  note: ${f.note}`);
      if (f.pr?.verification) {
        const v = f.pr.verification;
        lines.push(`  verification: CI ${v.ci?.status} · build ${v.build?.status} · canary ${v.canary?.status}${v.ready ? " · ready" : ""}`);
      }
      if (r.advisory) lines.push(`  ${r.advisory.id}: ${r.advisory.summary || ""}`);
      return lines.join("\n");
    }
    if (!r || !Array.isArray(r.findings)) return JSON.stringify(result, null, 2);
    const s = r.summary || {};
    const lines = [
      `Patches: ${s.open ?? 0} open findings, ${s.exposed ?? 0} on exposed workloads, ${s.exploited ?? 0} exploited; ${s.pr_now ?? 0} pull requests to open, ${s.pr_open ?? 0} open, ${s.waiting_approval ?? 0} waiting for you, ${s.deployed ?? 0} deployed`,
    ];
    if (r.coverage?.line) lines.push(`Inventory: ${r.coverage.line}`);
    if (r.feed) lines.push(`Feeds: ${r.feed.advisories ?? 0} advisories stored${r.feed.last_run ? `, last run ${r.feed.last_run}` : ", not run yet (compass patch feed run)"}`);
    const rows = [...r.findings].sort((a, b) => (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9));
    if (rows.length) lines.push("Findings:");
    for (const f of rows) lines.push(findingLine(f));
    if (!rows.length) lines.push(r.feed?.advisories ? "Nothing on the map is affected by what the feeds know." : "The feeds have not run yet.");
    return lines.join("\n");
  },
});

export const patchPrTool = defineTool({
  name: "compass_patch_pr",
  description:
    "Open or refresh the fix pull request of one repository now: every pending finding whose fix goes there, each a version string moved to the one the advisory names, verified by the repository's CI, an image build and a canary where configured. With approve=true the repository's findings waiting in the inbox are approved first, which is what accepting the inbox item does. A job; `compass_jobs get` follows it. Nothing merges: a person does.",
  input: {
    repo: z.string().describe("owner/name"),
    approve: z.boolean().optional().describe("Approve the repository's open findings that wait for a person before opening the pull request"),
  },
  scope: "operate",
  captain: "patch",
  annotations: { openWorldHint: true, idempotentHint: true },
  cli: { command: "patch pr", positionals: ["repo"], summary: "<owner/repo> [--approve]" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    let approved: any = null;
    if (args.approve) approved = await client.post("/api/patches/findings/approve", { repo: args.repo });
    const job = await client.post("/api/patches/pr", { repo: args.repo });
    ctx.log(`Queued the fix pull request for ${args.repo} (job ${job?.job_id}); the next tick opens or refreshes it.`);
    return approved ? { ...job, approved: approved.approved } : job;
  },
});

export const patchIgnoreTool = defineTool({
  name: "compass_patch_ignore",
  description:
    "Ignore one finding until a date, with a reason (mitigated elsewhere, not reachable, accepted): it leaves the feed and opens no pull request until then. Or reopen one that was ignored.",
  input: {
    key: z.string().describe("The finding's key (compass patch findings)"),
    until: z.string().optional().describe("YYYY-MM-DD; default 30 days"),
    days: z.number().int().min(1).max(365).optional(),
    reason: z.string().optional(),
    reopen: z.boolean().optional().describe("Reopen an ignored finding instead"),
  },
  scope: "operate",
  captain: "patch",
  annotations: { idempotentHint: true },
  cli: { command: "patch ignore", positionals: ["key"], summary: "<key> [--until YYYY-MM-DD | --days N] [--reason …] | --reopen" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    if (args.reopen) return client.post(`/api/patches/findings/${encodeURIComponent(args.key)}/reopen`, {});
    return client.post(`/api/patches/findings/${encodeURIComponent(args.key)}/ignore`, { until: args.until, days: args.days, reason: args.reason });
  },
});

export const patchFeedTool = defineTool({
  name: "compass_patch_feed",
  description:
    "The CVE feeds: status (each source's last run and cursor, the advisories stored, the inventory the feeds are asked about), run (the feeds now, the day a disclosure lands), tick (the match now: findings refreshed and the pull requests the policy decides). run and tick are jobs.",
  input: { action: z.enum(["status", "run", "tick"]).default("status") },
  scope: "operate",
  captain: "patch",
  annotations: { idempotentHint: true },
  cli: { command: "patch feed", positionals: ["action"], summary: "status | run | tick" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    if (args.action === "run") return client.post("/api/patches/feed/run", {});
    if (args.action === "tick") return client.post("/api/patches/tick", {});
    const all = (await client.get("/api/patches")) as any;
    return { feed: all?.feed, tick: all?.tick, coverage: all?.coverage };
  },
  render: (result) => {
    const r = result as any;
    if (r?.job_id) return `queued job ${r.job_id} (${r.command})`;
    const feed = r?.feed || {};
    const lines = [`${feed.advisories ?? 0} advisories stored${feed.last_run ? `; last run ${feed.last_run}` : "; not run yet"}`];
    for (const [name, s] of Object.entries<any>(feed.sources || {})) {
      const err = feed.report?.sources?.[name]?.error;
      lines.push(`  ${name.padEnd(10)} ${err ? `failed: ${err}` : s.last_success ? `ok ${s.last_success}` : "not yet"}`);
    }
    if (r?.coverage?.line) lines.push(`Inventory: ${r.coverage.line}`);
    if (r?.tick?.at) lines.push(`Last match: ${new Date(r.tick.at).toISOString()} (${r.tick.matched} findings matched)`);
    return lines.join("\n");
  },
});
