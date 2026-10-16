import { z } from "zod";
import { defineTool, plan } from "../core/registry.js";

const HONEYTOKEN_CLASSES = ["aws_credential", "db_credential", "ssh_key", "s3_object", "api_token", "dns_record"] as const;
const SEVERITIES = ["critical", "high", "medium", "low"] as const;

const hp = (id: string, tail = "") => `/api/honeypots/tokens/${encodeURIComponent(id)}${tail}`;

export const honeypotsTool = defineTool({
  name: "compass_honeypots",
  description:
    "Honeytokens: decoy credentials and resources that alert when touched. suggest = placements the infra map recommends (resource, class, priority, reason); list; get(id); material(id) reveals the lure and planting snippets (sensitive); alerts(id); incidents = open investigations; repos = candidate repos for plant PRs; settings = autopilot flag.",
  input: {
    action: z.enum(["suggest", "list", "get", "material", "alerts", "incidents", "repos", "settings"]),
    honeytoken_id: z.string().optional(),
    status: z.string().optional().describe("list: filter by status (pending_pr, armed, decommissioned…)"),
  },
  scope: "read",
  captain: "honeypot",
  sensitive: true,
  annotations: { readOnlyHint: true },
  cli: { command: "honeypots", positionals: ["action", "honeytoken_id"], summary: "suggest | list | get <id> | material <id> | alerts <id> | incidents | repos | settings" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    const need = () => {
      if (!args.honeytoken_id) throw new Error("honeytoken_id is required");
      return args.honeytoken_id;
    };
    switch (args.action) {
      case "suggest":
        return client.get("/api/honeypots/suggestions");
      case "list": {
        const items = (await client.get("/api/honeypots/tokens")) as any[];
        return items.filter((i) => !args.status || i.status === args.status);
      }
      case "get":
        return client.get(hp(need()));
      case "material":
        return client.get(hp(need(), "/material"));
      case "alerts":
        return client.get(hp(need(), "/alerts"));
      case "incidents":
        return client.get("/api/honeypots/incidents");
      case "repos":
        return client.get("/api/honeypots/repos");
      case "settings":
        return client.get("/api/honeypots/settings");
    }
  },
});

export const honeypotsPlantTool = defineTool({
  name: "compass_honeypots_plant",
  description:
    "Plant honeytokens. Either one explicit token (honeytoken_class, name, region, account_id, resource_path, severity, runbook, notify) or a batch from suggestions (suggestion_resource_ids, or top_n to take the highest-priority suggestions). Each plant opens a Terraform PR in the honeypot repo; the token arms once merged. Returns a plan unless confirm=true. The response for a single plant includes the lure material (sensitive).",
  input: {
    honeytoken_class: z.enum(HONEYTOKEN_CLASSES).optional(),
    name: z.string().optional().describe("Cover name; empty picks a realistic one"),
    description: z.string().optional(),
    account_id: z.string().optional(),
    region: z.string().optional(),
    resource_path: z.string().optional(),
    severity: z.enum(SEVERITIES).optional().describe("default high"),
    runbook: z.string().optional(),
    auto_block_ip: z.boolean().optional(),
    notification_endpoint_ids: z.array(z.string()).optional().describe("Endpoints to alert; default all configured"),
    suggestion_resource_ids: z.array(z.string()).optional().describe("Batch: resource ids from compass_honeypots suggest"),
    top_n: z.number().int().min(1).max(50).optional().describe("Batch: take the N highest-priority suggestions"),
    confirm: z.boolean().optional(),
  },
  scope: "operate",
  captain: "honeypot",
  confirm: true,
  sensitive: true,
  annotations: { openWorldHint: true },
  cli: { command: "honeypots plant", summary: "Plant one token (--honeytoken-class …) or a batch (--top-n / --suggestion-resource-ids) with --yes" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    const endpoints = args.notification_endpoint_ids?.length
      ? args.notification_endpoint_ids
      : ((await client.get("/api/notifications/endpoints")) as any[]).map((e) => e.endpoint_id);
    const batch = !!(args.suggestion_resource_ids?.length || args.top_n);

    if (batch) {
      const suggestions = (await client.get("/api/honeypots/suggestions")) as any[];
      const chosen = args.suggestion_resource_ids?.length
        ? suggestions.filter((s) => args.suggestion_resource_ids!.includes(s.resource_id))
        : [...suggestions].sort((a, b) => (a.priority ?? 99) - (b.priority ?? 99)).slice(0, args.top_n);
      if (!chosen.length) throw new Error("No matching suggestions; run compass_honeypots suggest first");
      if (!args.confirm) {
        return plan(
          `Plant ${chosen.length} honeytoken(s) from map suggestions`,
          chosen.map((s) => `${s.recommended_class} near ${s.resource_name || s.resource_id} (${s.region || "?"}): ${s.reason || ""}`),
          { suggestions: chosen, notification_endpoint_ids: endpoints },
          endpoints.length ? undefined : ["No notification endpoints configured; alerts would go nowhere (compass_notify_endpoints add)."],
        );
      }
      const result = await client.post("/api/honeypots/autopilot", {
        suggestions: chosen.map((s) => ({
          resource_id: s.resource_id,
          recommended_class: s.recommended_class,
          resource_name: s.resource_name,
          account_id: s.account_id,
          region: s.region,
          description: s.description || s.reason,
          default_config: s.default_config,
        })),
        notification_endpoint_ids: endpoints,
      });
      return { executed: true, ...result };
    }

    if (!args.honeytoken_class) throw new Error("honeytoken_class is required (or use top_n / suggestion_resource_ids for a batch)");
    const body = {
      honeytoken_class: args.honeytoken_class,
      name: args.name || "",
      description: args.description || "",
      account_id: args.account_id,
      region: args.region,
      resource_path: args.resource_path,
      action_config: {
        severity: args.severity || "high",
        runbook: args.runbook || "",
        notification_endpoint_ids: endpoints,
        auto_block_ip: args.auto_block_ip ?? false,
      },
    };
    if (!args.confirm) {
      return plan(
        `Plant one ${args.honeytoken_class} honeytoken${args.name ? ` named ${args.name}` : ""}`,
        ["Generate lure material", "Open a Terraform PR adding the decoy and its alerting", `Alert ${endpoints.length} endpoint(s) at severity ${body.action_config.severity} when touched`],
        { request: body },
      );
    }
    const created = await client.post("/api/honeypots/tokens", body);
    ctx.log("Lure material is included once in this response; store it where the decoy should be found.");
    return { executed: true, ...created };
  },
});

export const honeypotsManageTool = defineTool({
  name: "compass_honeypots_manage",
  description:
    "Honeytoken lifecycle. arm(id) marks it live after the PR merged; test_fire(id) sends a synthetic alert through the real notification path (pages people, confirm first); resolve_alert(id, alert_id); set_placement(id, placement_type, …) records where the lure lives; clear_placement(id); update(id, name/description/…); set_repo(repo) picks the default honeypot repo ('' = auto from map); set_autopilot(enabled) plants every suggestion automatically.",
  input: {
    action: z.enum(["arm", "test_fire", "resolve_alert", "set_placement", "clear_placement", "update", "set_repo", "set_autopilot"]),
    honeytoken_id: z.string().optional(),
    alert_id: z.string().optional(),
    placement_type: z.enum(["ecs_task_env", "ecs_task_secret", "secrets_manager", "docker_image", "file", "manual"]).optional(),
    placement_detail: z.string().optional(),
    placement_notes: z.string().optional(),
    name: z.string().optional(),
    description: z.string().optional(),
    region: z.string().optional(),
    account_id: z.string().optional(),
    resource_path: z.string().optional(),
    repo: z.string().optional().describe("set_repo: owner/name or '' for auto"),
    enabled: z.boolean().optional().describe("set_autopilot"),
    confirm: z.boolean().optional().describe("test_fire only"),
  },
  scope: "operate",
  captain: "honeypot",
  annotations: { idempotentHint: true },
  cli: { command: "honeypots manage", positionals: ["action", "honeytoken_id", "alert_id"], summary: "arm <id> | test_fire <id> --yes | resolve_alert <id> <alert> | set_placement <id> … | set_repo --repo | set_autopilot --enabled" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    const need = () => {
      if (!args.honeytoken_id) throw new Error("honeytoken_id is required");
      return args.honeytoken_id;
    };
    switch (args.action) {
      case "arm":
        return client.post(hp(need(), "/arm"));
      case "test_fire":
        if (!args.confirm) {
          return plan(`Test-fire honeytoken ${need()}`, ["Write a synthetic alert (flagged as test)", "Notify every endpoint configured on the token"], {}, ["This pages real people. Pass confirm=true to proceed."]);
        }
        return client.post(hp(need(), "/test-fire"));
      case "resolve_alert":
        if (!args.alert_id) throw new Error("alert_id is required");
        return client.post(hp(need(), `/alerts/${encodeURIComponent(args.alert_id)}/resolve`));
      case "set_placement":
        if (!args.placement_type) throw new Error("placement_type is required");
        return client.put(hp(need(), "/placement"), { type: args.placement_type, detail: args.placement_detail, notes: args.placement_notes });
      case "clear_placement":
        return client.delete(hp(need(), "/placement"));
      case "update":
        return client.put(hp(need()), { name: args.name, description: args.description, region: args.region, account_id: args.account_id, resource_path: args.resource_path });
      case "set_repo":
        return client.post("/api/honeypots/repo-default", { repo: args.repo ?? "" });
      case "set_autopilot":
        if (args.enabled === undefined) throw new Error("enabled is required");
        return client.put("/api/honeypots/settings", { autopilot: args.enabled });
    }
  },
});

export const honeypotsRunbookTool = defineTool({
  name: "compass_honeypots_runbook",
  description: "Send a honeytoken's runbook to notification endpoints now (incident response). Returns a plan unless confirm=true because it pages people.",
  input: {
    honeytoken_id: z.string(),
    runbook: z.string().optional().describe("Override the stored runbook text"),
    notification_endpoint_ids: z.array(z.string()).optional(),
    confirm: z.boolean().optional(),
  },
  scope: "operate",
  captain: "honeypot",
  confirm: true,
  annotations: { openWorldHint: true },
  cli: { command: "honeypots runbook", positionals: ["honeytoken_id"], summary: "Dispatch the runbook to endpoints (--yes)" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    const endpoints = args.notification_endpoint_ids?.length ? args.notification_endpoint_ids : ((await client.get("/api/notifications/endpoints")) as any[]).map((e) => e.endpoint_id);
    if (!args.confirm) return plan(`Dispatch runbook for ${args.honeytoken_id}`, [`Notify ${endpoints.length} endpoint(s)`], { notification_endpoint_ids: endpoints });
    const result = await client.post(hp(args.honeytoken_id, "/execute-runbook"), { runbook: args.runbook || "", notification_endpoint_ids: endpoints });
    return { executed: true, ...result };
  },
});

export const honeypotsDecommissionTool = defineTool({
  name: "compass_honeypots_decommission",
  description: "Retire honeytokens: opens a removal PR per token and marks them decommissioned. Pass honeytoken_ids, or all=true for every armed token. Returns a plan unless confirm=true.",
  input: {
    honeytoken_ids: z.array(z.string()).optional(),
    all: z.boolean().optional(),
    confirm: z.boolean().optional(),
  },
  scope: "operate",
  captain: "honeypot",
  confirm: true,
  annotations: { destructiveHint: true, openWorldHint: true },
  cli: { command: "honeypots decommission", positionals: ["honeytoken_ids"], summary: "Retire tokens via removal PRs (--all, --yes)" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    let ids = args.honeytoken_ids || [];
    if (args.all) {
      const items = (await client.get("/api/honeypots/tokens")) as any[];
      ids = items.filter((i) => i.status !== "decommissioned").map((i) => i.honeytoken_id);
    }
    if (!ids.length) throw new Error("honeytoken_ids or all=true is required");
    if (!args.confirm) return plan(`Decommission ${ids.length} honeytoken(s)`, ids.map((id) => `Open removal PR for ${id} and mark it decommissioned`), { honeytoken_ids: ids });
    const results = [];
    for (const id of ids) {
      try {
        results.push({ honeytoken_id: id, ...(await client.delete(hp(id))) });
      } catch (err: any) {
        results.push({ honeytoken_id: id, error: err.message });
      }
    }
    return { executed: true, results };
  },
});

export const honeypotsIncidentTool = defineTool({
  name: "compass_honeypots_incident",
  description: "Investigate a honeytoken incident. get(id) detail; summarize(id) asks the model for a narrative; report(id); approve(id, action_id) runs an approved response action; close(id).",
  input: {
    action: z.enum(["get", "summarize", "report", "approve", "close"]),
    incident_id: z.string(),
    action_id: z.string().optional(),
    force: z.boolean().optional().describe("summarize: regenerate"),
  },
  scope: "operate",
  captain: "honeypot",
  annotations: {},
  cli: { command: "honeypots incident", positionals: ["action", "incident_id", "action_id"], summary: "get | summarize | report | approve <id> <action> | close" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    const base = `/api/honeypots/incidents/${encodeURIComponent(args.incident_id)}`;
    switch (args.action) {
      case "get":
        return client.get(base);
      case "summarize":
        return client.request("POST", `${base}/summarize`, { params: { force: args.force ?? false }, timeoutMs: 5 * 60_000 });
      case "report":
        return client.get(`${base}/report`);
      case "approve":
        if (!args.action_id) throw new Error("action_id is required");
        return client.post(`${base}/approve`, { action_id: args.action_id });
      case "close":
        return client.post(`${base}/close`);
    }
  },
});
