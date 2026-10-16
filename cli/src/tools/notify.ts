import { z } from "zod";
import { defineTool } from "../core/registry.js";

export const notifyEndpointsTool = defineTool({
  name: "compass_notify_endpoints",
  description:
    "Notification endpoints used for honeypot alerts and runbooks. list; add(type beamreach_email|email|slack|webhook|pagerduty|smtp, ...); test(endpoint_id) sends a test message; remove(endpoint_id). beamreach_email needs only recipients (Beamreach relays via its own SES). slack takes webhook_url, or bot_token plus channel. pagerduty takes routing_key.",
  input: {
    action: z.enum(["list", "add", "test", "remove"]),
    endpoint_id: z.string().optional(),
    name: z.string().optional(),
    type: z.enum(["beamreach_email", "email", "slack", "webhook", "pagerduty", "smtp"]).optional(),
    recipients: z.array(z.string()).optional().describe("email types"),
    webhook_url: z.string().optional().describe("slack incoming webhook or generic webhook URL"),
    channel: z.string().optional().describe("slack channel"),
    bot_token: z.string().optional().describe("slack bot token (xoxb-…)"),
    routing_key: z.string().optional().describe("pagerduty Events v2 key"),
    ses_source_email: z.string().optional(),
    ses_region: z.string().optional(),
    host: z.string().optional().describe("smtp"),
    port: z.number().int().optional(),
    username: z.string().optional(),
    password: z.string().optional(),
    from_address: z.string().optional(),
    use_tls: z.boolean().optional(),
    use_ssl: z.boolean().optional(),
  },
  scope: "operate",
  annotations: { destructiveHint: true, openWorldHint: true },
  cli: { command: "notify", positionals: ["action", "endpoint_id"], summary: "list | add --type --name ... | test <id> | remove <id>" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    switch (args.action) {
      case "list":
        return client.get("/api/notifications/endpoints");
      case "add": {
        if (!args.type) throw new Error("type is required");
        const { action, endpoint_id, ...rest } = args;
        const name = args.name || `${args.type}${args.channel ? ` ${args.channel}` : ""}${args.recipients?.length ? ` ${args.recipients[0]}` : ""}`;
        return client.post("/api/notifications/endpoints", { ...rest, name });
      }
      case "test":
        if (!args.endpoint_id) throw new Error("endpoint_id is required");
        return client.post(`/api/notifications/endpoints/${encodeURIComponent(args.endpoint_id)}/test`);
      case "remove":
        if (!args.endpoint_id) throw new Error("endpoint_id is required");
        return client.delete(`/api/notifications/endpoints/${encodeURIComponent(args.endpoint_id)}`);
    }
  },
});

export const sesSenderTool = defineTool({
  name: "compass_notify_ses_sender",
  description: "Custom SES sender for 'email' endpoints (must be a verified identity in the install account). get, set(ses_source_email, ses_region), clear.",
  input: {
    action: z.enum(["get", "set", "clear"]),
    ses_source_email: z.string().optional(),
    ses_region: z.string().optional(),
  },
  scope: "admin",
  annotations: { idempotentHint: true },
  cli: { command: "notify ses-sender", positionals: ["action", "ses_source_email"], summary: "get | set <email> | clear" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    if (args.action === "get") return client.get("/api/notifications/ses-config");
    if (args.action === "clear") return client.post("/api/notifications/ses-config", { ses_source_email: null, ses_region: null });
    if (!args.ses_source_email) throw new Error("ses_source_email is required");
    return client.post("/api/notifications/ses-config", { ses_source_email: args.ses_source_email, ses_region: args.ses_region });
  },
});
