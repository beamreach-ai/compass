import { z } from "zod";
import { defineTool } from "../core/registry.js";
import { waitForJob } from "../core/poll.js";

export const mapTool = defineTool({
  name: "compass_map",
  description:
    "Infrastructure map (AWS resources, repos, Terraform modules and their relationships). run = full re-map (needs AWS connected; GitHub optional); status = scan state; get = graph or a subgraph around focus; owner = owning repo/module of a resource; tf_location = where a resource is defined in Terraform.",
  input: {
    action: z.enum(["run", "status", "get", "owner", "tf_location"]),
    resource_id: z.string().optional().describe("owner / tf_location: resource id or ARN; get: alias for focus"),
    focus: z.string().optional().describe("get: center the subgraph on this resource"),
    depth: z.number().int().min(1).max(6).optional().describe("get: traversal depth around focus (default 2)"),
    direction: z.enum(["incoming", "outgoing", "both"]).optional(),
    limit: z.number().int().min(100).max(10000).optional().describe("get: max items for the full graph (default 2000)"),
    org_id: z.string().optional(),
    wait: z.boolean().optional().describe("run: block until the map job finishes"),
  },
  scope: "read",
  captain: "mapper",
  annotations: { readOnlyHint: false, idempotentHint: true },
  cli: { command: "map", positionals: ["action", "resource_id"], summary: "run | status | get [--focus] | owner <id> | tf-location <id>" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    switch (args.action) {
      case "run": {
        const job = await client.post("/api/maps/reindex", { mode: "full", org_id: args.org_id });
        if (job.existing) ctx.log(`A map job is already running: ${job.job_id}`);
        if (!args.wait) return job;
        return waitForJob(client, job.job_id, {
          onProgress: (j) => {
            const last = j.events?.[j.events.length - 1];
            if (last) ctx.log(`[mapper] ${j.status}: ${last.message}`);
          },
        });
      }
      case "status": {
        const bootstrap = await client.get("/api/settings/bootstrap");
        return bootstrap?.infra_scan ?? bootstrap;
      }
      case "get": {
        const graph = await client.get("/api/maps", {
          org_id: args.org_id,
          focus: args.focus || args.resource_id,
          depth: args.depth,
          direction: args.direction,
          limit: args.limit,
        });
        return graph;
      }
      case "owner":
        if (!args.resource_id) throw new Error("resource_id is required");
        return client.get("/api/maps/ownership", { resource_id: args.resource_id, org_id: args.org_id });
      case "tf_location":
        if (!args.resource_id) throw new Error("resource_id is required");
        return client.get("/api/librarian/terraform-location", { resource_id: args.resource_id, org_id: args.org_id });
    }
  },
  render: (result) => {
    const r = result as any;
    if (r && Array.isArray(r.nodes) && Array.isArray(r.edges)) {
      const types: Record<string, number> = {};
      for (const n of r.nodes) types[n.resource_type || n.type || "?"] = (types[n.resource_type || n.type || "?"] || 0) + 1;
      const top = Object.entries(types).sort((a, b) => b[1] - a[1]).slice(0, 15).map(([t, c]) => `  ${c}\t${t}`);
      return [`${r.nodes.length} nodes, ${r.edges.length} edges`, ...top, r.nodes.length > 0 ? "(use --json for the full graph)" : ""].join("\n");
    }
    return JSON.stringify(r, null, 2);
  },
});
