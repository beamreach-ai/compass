import { describe, expect, it } from "vitest";
import { buildProgram } from "../src/cli/program.js";
import { connection, fakeEngine } from "./helpers.js";

function run(argv: string[], routes: Record<string, unknown>) {
  const engine = fakeEngine(routes);
  const stdout: string[] = [];
  const stderr: string[] = [];
  let exitCode: number | undefined;
  const program = buildProgram({
    fetchImpl: engine.fetchImpl,
    connection,
    plugins: [],
    stdout: (t) => stdout.push(t),
    stderr: (t) => stderr.push(t),
    exit: (code) => {
      exitCode = code;
    },
  });
  program.exitOverride();
  return program.parseAsync(["node", "compass", ...argv]).then(() => ({ stdout: stdout.join("\n"), stderr: stderr.join("\n"), exitCode, calls: engine.calls }));
}

const finding = {
  key: "k1", advisory_id: "CVE-2026-109618", package: "php", version: "8.3.30", fixed_version: "8.3.36", severity: "critical", cvss: 9.2, kev: false, epss: 0.43,
  repo: "acme/web", path: "Dockerfile", node_name: "web/Dockerfile", exposed: true, state: "pr_open", verdict: "pr", verdict_reasons: ["CVSS 9.2 ≥ 7.0", "the workload faces the internet"],
  workloads: [{ node_id: "td-web", name: "web:41", exposed: true }], pr: { url: "https://github.com/acme/web/pull/9", number: 9, verification: { ci: { status: "passed" }, build: { status: "passed" }, canary: { status: "skipped" }, ready: true } },
};
const patches = {
  summary: { open: 3, exposed: 1, exploited: 0, pr_now: 1, pr_open: 1, waiting_approval: 1, deployed: 2 },
  findings: [finding, { ...finding, key: "k2", severity: "medium", cvss: 5.0, state: "open", verdict: "inbox", exposed: false, workloads: [], fixed_version: null, pr: null }],
  coverage: { line: "1 of 1 images resolved" },
  feed: { advisories: 12, last_run: "2026-10-25T10:00:00Z", sources: { osv: { last_success: "2026-10-25T10:00:00Z" }, kev: { last_success: "2026-10-25T10:00:00Z" } }, report: { sources: { ubuntu: { error: "ReadTimeout" } } } },
  tick: { at: 1_792_670_000_000, matched: 3 },
};

describe("compass patch", () => {
  it("lists the findings with the fixed versions, the workloads and the pull requests", async () => {
    const r = await run(["patch", "findings", "--exposed"], { "GET /api/patches": patches });
    expect(r.calls[0].query).toEqual({ exposed: "true" });
    expect(r.stdout).toContain("Patches: 3 open findings, 1 on exposed workloads, 0 exploited; 1 pull requests to open, 1 open, 1 waiting for you, 2 deployed");
    expect(r.stdout).toContain("Inventory: 1 of 1 images resolved");
    expect(r.stdout).toMatch(/k1\s+critical\s+CVE-2026-109618\s+php 8\.3\.30 → 8\.3\.36\s+web:41 \(exposed\)\s+pull request https:\/\/github\.com\/acme\/web\/pull\/9\s+\[CVS/);
    expect(r.stdout).toContain("no fix yet");
    expect(r.stdout).toContain("waits for you");
  });

  it("shows one finding with its verification and advisory", async () => {
    const r = await run(["patch", "findings", "k1"], { "GET /api/patches/findings/k1": { finding, advisory: { id: "CVE-2026-109618", summary: "php2shell" } } });
    expect(r.stdout).toContain("verification: CI passed · build passed · canary skipped · ready");
    expect(r.stdout).toContain("why: CVSS 9.2 ≥ 7.0; the workload faces the internet");
    expect(r.stdout).toContain("CVE-2026-109618: php2shell");
  });

  it("opens the pull request as a job, approving the repository's findings first when asked", async () => {
    const r = await run(["patch", "pr", "acme/web", "--approve", "--json"], {
      "POST /api/patches/findings/approve": { approved: 2, keys: ["k2", "k3"] },
      "POST /api/patches/pr": { job_id: "j-9", status: "queued", command: "pr", repo: "acme/web" },
    });
    expect(r.calls.map((c) => `${c.method} ${c.path}`)).toEqual(["POST /api/patches/findings/approve", "POST /api/patches/pr"]);
    expect(r.calls[0].body).toEqual({ repo: "acme/web" });
    expect(JSON.parse(r.stdout)).toEqual({ job_id: "j-9", status: "queued", command: "pr", repo: "acme/web", approved: 2 });
  });

  it("ignores a finding with a reason and can reopen it", async () => {
    const r = await run(["patch", "ignore", "k2", "--days", "14", "--reason", "behind the WAF", "--json"], { "POST /api/patches/findings/k2/ignore": { ignored: 1, until: "2026-11-08" } });
    expect(r.calls[0].body).toEqual({ days: 14, reason: "behind the WAF" });
    const back = await run(["patch", "ignore", "k2", "--reopen", "--json"], { "POST /api/patches/findings/k2/reopen": { state: "open" } });
    expect(back.calls[0].path).toBe("/api/patches/findings/k2/reopen");
  });

  it("the feeds: status renders each source, run and tick are jobs", async () => {
    const status = await run(["patch", "feed"], { "GET /api/patches": patches });
    expect(status.stdout).toContain("12 advisories stored; last run 2026-10-25T10:00:00Z");
    expect(status.stdout).toMatch(/osv\s+ok 2026-10-25T10:00:00Z/);
    expect(status.stdout).toContain("Last match: 2026-10-22T");
    const ran = await run(["patch", "feed", "run"], { "POST /api/patches/feed/run": { job_id: "j-1", status: "queued", command: "feed_run" } });
    expect(ran.stdout).toBe("queued job j-1 (feed_run)");
    const ticked = await run(["patch", "feed", "tick", "--json"], { "POST /api/patches/tick": { job_id: "j-2", status: "queued", command: "tick" } });
    expect(ticked.calls[0].path).toBe("/api/patches/tick");
  });
});

const inbox = {
  generated_at: 1, counts: { total: 2, urgent: 1, by_tab: { honeypots: 1, patches: 1 }, by_verb: { approve: 2, fix: 0, merge: 0, later: 0 } },
  items: [
    { id: "decoy.approval:i1:ecs_restart_task", kind: "decoy.approval", verb: "approve", severity: "urgent", captain: "honeypot", tab: "honeypots", title: "Restart the task on payments-api", summary: "risk 3",
      since: Date.now() - 120_000, subject: {}, detail: { risk: { score: 3 } },
      actions: [{ key: "run", label: "Run", method: "POST", path: "/api/honeypots/incidents/i1/approve", body: { action_id: "a1" }, scope: "operate", confirm: "Runs ecs_restart_task on payments-api (risk 3)." },
                { key: "open", label: "Open incident", href: "/workspace?tab=honeypots" }] },
    { id: "patch.approve:acme/web", kind: "patch.approve", verb: "approve", severity: "normal", captain: "patch", tab: "patches", title: "Open the pull request for 1 finding in acme/web", summary: "CVE-2026-20001",
      since: Date.now() - 3_600_000, subject: { repo: "acme/web" }, detail: {},
      actions: [{ key: "approve", label: "Approve the pull request", method: "POST", path: "/api/patches/findings/approve", body: { keys: ["k2"] }, scope: "operate", confirm: "Marks 1 finding approved." }] },
  ],
};

describe("compass inbox", () => {
  it("lists what needs a person with the command that acts on each", async () => {
    const r = await run(["inbox", "--tab", "patches"], { "GET /api/inbox": inbox });
    expect(r.calls[0].query).toEqual({ tab: "patches" });
    expect(r.stdout).toContain("Needs you: 2 (1 urgent): 2 to approve");
    expect(r.stdout).toMatch(/urgent\s+approve\s+Restart the task on payments-api/);
    expect(r.stdout).toContain("→ compass inbox act decoy.approval:i1:ecs_restart_task run");
  });

  it("act shows the confirm sentence and exits 2; with --yes it calls the item's route", async () => {
    const planned = await run(["inbox", "act", "decoy.approval:i1:ecs_restart_task"], { "GET /api/inbox": inbox });
    expect(planned.exitCode).toBe(2);
    expect(planned.stdout).toContain("Runs ecs_restart_task on payments-api (risk 3).");
    expect(planned.calls.find((c) => c.path === "/api/honeypots/incidents/i1/approve")).toBeUndefined();
    const done = await run(["inbox", "act", "decoy.approval:i1:ecs_restart_task", "run", "--yes", "--json"], { "GET /api/inbox": inbox, "POST /api/honeypots/incidents/i1/approve": { status: "verified" } });
    const call = done.calls.find((c) => c.path === "/api/honeypots/incidents/i1/approve");
    expect(call?.method).toBe("POST");
    expect(call?.body).toEqual({ action_id: "a1" });
    expect(JSON.parse(done.stdout)).toEqual({ status: "verified" });
    const link = await run(["inbox", "act", "decoy.approval:i1:ecs_restart_task", "open", "--json"], { "GET /api/inbox": inbox });
    expect(JSON.parse(link.stdout).href).toBe("/workspace?tab=honeypots");
    const gone = await run(["inbox", "act", "nope", "--yes"], { "GET /api/inbox": inbox });
    expect(gone.exitCode).toBe(1);
    expect(gone.stderr).toContain("no inbox item nope");
  });

  it("snoozes and un-snoozes", async () => {
    const r = await run(["inbox", "snooze", "job.failed:j1", "--json"], { "POST /api/inbox/job.failed:j1/snooze": { snoozed: true } });
    expect(r.calls[0].method).toBe("POST");
    const back = await run(["inbox", "snooze", "job.failed:j1", "--unsnooze", "--json"], { "DELETE /api/inbox/job.failed:j1/snooze": { snoozed: false } });
    expect(back.calls[0].method).toBe("DELETE");
  });
});
