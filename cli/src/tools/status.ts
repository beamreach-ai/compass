import { defineTool } from "../core/registry.js";
import { CLI_VERSION, SUPPORTED_API_VERSIONS } from "../version.js";

export const statusTool = defineTool({
  name: "compass_status",
  description:
    "One-screen brief of this Compass install: target and GitHub connection, infra scan state, findings by severity and domain, active jobs, open PRs, honeypots, usage and plan. Call this first in any session.",
  input: {},
  scope: "read",
  annotations: { readOnlyHint: true },
  cli: { command: "status", summary: "One-screen brief of the install" },
  handler: async (_args, ctx) => ctx.client().get("/api/status"),
  render: (result) => {
    const s = result as any;
    const lines: string[] = [];
    lines.push(`Compass ${s.compass_version} (api ${s.api_version})  workspace ${s.workspace?.workspace_id}  mode ${s.app?.mode}${s.app?.read_only ? " (read-only)" : ""}`);
    // compass_core.updates: is a release out, and does the compose file ask for what runs
    const u = s.update || {};
    if (u.available && u.latest) {
      const how = u.apply === "updater" ? "one click on Settings → Updates" : "Settings → Updates shows the two commands";
      lines.push(`Update: Compass ${u.latest} is available (this is ${u.running}); ${how}`);
      // the notes feed's releases between the two, newest first: one line each, the page has the rest
      const notes: any[] = Array.isArray(u.release_notes) ? u.release_notes : [];
      if (notes.length) lines.push("What's new:");
      for (const n of notes) lines.push(`  ${n.version}${n.headline || n.title ? ` — ${n.headline || n.title}` : ""}${n.link ? `  ${n.link}` : ""}`);
    }
    if (u.pinned_differs) lines.push(`Update: the compose file asks for ${u.pinned} but ${u.running} is running; run \`docker compose up -d\``);
    const t = s.target || {};
    const access = s.access || {};
    lines.push(
      `AWS: ${t.aws_connected ? `account ${t.aws?.account_id || "?"} (${t.aws?.region || "?"}) · read ${t.aws?.verified_at ? "✓" : "unverified"}` : "not connected: `compass account connect`"}` +
        (access.actions_total != null ? ` · ${access.actions_armed ?? 0} of ${access.actions_total} actions armed` : "") +
        (access.audited_at ? ` · audited ${access.audited_at}` : ""),
    );
    lines.push(`GitHub: ${t.github_connected ? `connected (${t.github?.login || "app"}, ${t.github?.repositories_count ?? "?"} repos)` : "not connected"}`);
    const scan = s.infra_scan || {};
    lines.push(`Infra map: ${scan.scanned ? "scanned" : scan.status || "pending"}${scan.latest_message ? ` — ${scan.latest_message}` : ""}`);
    const f = s.findings || {};
    if (f.by_domain) {
      const sec = f.by_domain.security || {};
      const fin = f.by_domain.finops || {};
      const finTotal = Object.values(fin).reduce((a: number, b: any) => a + (Number(b) || 0), 0);
      lines.push(`Security findings: ${sec.critical || 0} critical, ${sec.high || 0} high, ${sec.medium || 0} medium, ${sec.low || 0} low`);
      lines.push(`FinOps findings: ${finTotal} (est. $${f.estimated_monthly_savings_usd ?? 0}/mo)`);
    }
    const j = s.jobs || {};
    lines.push(`Jobs: ${j.active_count ?? 0} active${j.recent_failures?.length ? `, ${j.recent_failures.length} recent failures` : ""}`);
    const p = s.prs || {};
    lines.push(`PRs opened by Compass: ${p.opened ?? 0}${p.pending ? ` (${p.pending} pending)` : ""}`);
    const h = s.honeypots || {};
    lines.push(`Honeypots: ${h.total ?? 0} (${Object.entries(h.by_status || {}).map(([k, v]) => `${v} ${k}`).join(", ") || "none"}), ${h.active_alerts ?? 0} active alerts`);
    const b = s.billing || {};
    if (b.plan) lines.push(`Plan: ${b.plan}`);
    if (s.errors && Object.keys(s.errors).length) lines.push(`Sections unavailable: ${Object.keys(s.errors).join(", ")}`);
    return lines.join("\n");
  },
});

export const doctorTool = defineTool({
  name: "compass_doctor",
  description:
    "Check the connection to Compass: endpoint reachable, token valid and its scope, API version compatible with this CLI. Use when any other call fails unexpectedly.",
  input: {},
  scope: "read",
  annotations: { readOnlyHint: true },
  cli: { command: "doctor", summary: "Check connection, token and version compatibility" },
  exitCode: (result) => ((result as any)?.ok ? 0 : 1),
  handler: async (_args, ctx) => {
    const checks: Array<{ check: string; ok: boolean; detail: string }> = [];
    const conn = ctx.connection;
    checks.push({
      check: "profile",
      ok: !!conn,
      detail: conn ? `${conn.profileName || "(env)"} → ${conn.url}` : "no profile yet; run `compass install` (new account) or `compass login --from-aws` (existing stack)",
    });
    if (!conn) return { ok: false, cli_version: CLI_VERSION, checks };
    const client = ctx.client();
    try {
      // Through the load balancer /health belongs to the dashboard; /api/auth/me is the engine's public endpoint.
      const probe = await client.request<Response>("GET", "/api/auth/me", { raw: true, timeoutMs: 10_000 });
      const ok = probe.status < 500;
      checks.push({ check: "reachable", ok, detail: ok ? `engine answered ${probe.status} at ${client.baseUrl}` : `engine returned ${probe.status}` });
      if (!ok) return { ok: false, cli_version: CLI_VERSION, checks };
    } catch (err: any) {
      checks.push({ check: "reachable", ok: false, detail: err.message });
      return { ok: false, cli_version: CLI_VERSION, checks };
    }
    try {
      const me = await client.get("/api/auth/me");
      const viaToken = me?.method === "api_token";
      checks.push({
        check: "token",
        ok: true,
        detail: viaToken ? `${me.kind} token, scope ${me.scope}, workspace ${me.workspace_id}` : `no token in use (${me.auth_enabled ? "session auth enabled" : "open install"})`,
      });
    } catch (err: any) {
      checks.push({ check: "token", ok: false, detail: err.message });
    }
    try {
      const status = await client.get("/api/status");
      const compatible = SUPPORTED_API_VERSIONS.includes(status.api_version);
      checks.push({
        check: "api_version",
        ok: compatible,
        detail: `engine ${status.api_version} (compass ${status.compass_version}); CLI ${CLI_VERSION} supports ${SUPPORTED_API_VERSIONS.join(", ")}`,
      });
    } catch (err: any) {
      checks.push({ check: "api_version", ok: false, detail: `GET /api/status failed: ${err.message} (engine may predate API tokens)` });
    }
    return { ok: checks.every((c) => c.ok), cli_version: CLI_VERSION, checks };
  },
});
