import { z } from "zod";
import { defineTool, plan } from "../core/registry.js";

export const iacTool = defineTool({
  name: "compass_iac",
  description:
    "Infrastructure-as-code coverage from the map. coverage = totals and per-type breakdown; unmanaged = resources with no Terraform; drifted = resources whose live state differs from state; suggestions = repos and module paths to import into.",
  input: {
    action: z.enum(["coverage", "unmanaged", "drifted", "suggestions"]),
    resource_type: z.string().optional().describe("filter, e.g. aws_s3_bucket"),
    search: z.string().optional().describe("substring on name/id"),
    limit: z.number().int().min(1).max(5000).optional(),
    org_id: z.string().optional(),
  },
  scope: "read",
  captain: "iac",
  annotations: { readOnlyHint: true },
  cli: { command: "iac", positionals: ["action"], summary: "coverage | unmanaged | drifted | suggestions" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    if (args.action === "suggestions") return client.get("/api/iac/suggestions", { org_id: args.org_id });
    const coverage = await client.get("/api/iac/coverage", { org_id: args.org_id });
    if (args.action === "coverage") {
      const { unmanaged, drifted, ...rest } = coverage;
      return { ...rest, unmanaged_count: unmanaged?.length ?? rest.unmanaged_count, drifted_count: drifted?.length ?? rest.drifted_count };
    }
    const list = (coverage[args.action] || []) as any[];
    const q = args.search?.toLowerCase();
    return list
      .filter((r) => !args.resource_type || (r.resource_type || r.type) === args.resource_type)
      .filter((r) => !q || JSON.stringify(r).toLowerCase().includes(q))
      .slice(0, args.limit ?? 200);
  },
});

export const iacImportTool = defineTool({
  name: "compass_iac_import",
  description: "Open a pull request that imports unmanaged resources into Terraform (generated config, rewired references, plan verified as imports-only). Returns a plan unless confirm=true.",
  input: {
    resource_ids: z.array(z.string()).min(1),
    repo: z.string().describe("owner/name"),
    branch: z.string().optional().describe("base branch, default main"),
    module_path: z.string().optional().describe("existing module path, or a new one with is_new_module"),
    is_new_module: z.boolean().optional(),
    org_id: z.string().optional(),
    confirm: z.boolean().optional(),
  },
  scope: "operate",
  captain: "iac",
  confirm: true,
  annotations: { openWorldHint: true },
  cli: { command: "iac import", positionals: ["resource_ids"], summary: "Import resources into Terraform via PR (--repo, --yes)" },
  handler: async (args, ctx) => {
    if (!args.confirm) {
      return plan(
        `Import ${args.resource_ids.length} resource(s) into ${args.repo}${args.module_path ? ` at ${args.module_path}` : ""} (branch ${args.branch || "main"})`,
        [
          "Clone the repo and run terraform to generate configuration for the resources",
          "Strip read-only fields, scrub secrets, rewire literals to existing references",
          "Verify `terraform plan` shows imports only",
          "Open a pull request; nothing is applied",
        ],
        { resource_ids: args.resource_ids, repo: args.repo, branch: args.branch || "main", module_path: args.module_path },
      );
    }
    const result = await ctx.client().post("/api/iac/import", {
      resource_ids: args.resource_ids,
      repo: args.repo,
      branch: args.branch || "main",
      module_path: args.module_path,
      is_new_module: args.is_new_module ?? false,
      org_id: args.org_id,
    });
    return { executed: true, ...result };
  },
});

export const iacFixDriftTool = defineTool({
  name: "compass_iac_fix_drift",
  description: "Open pull requests that bring Terraform back in line with the live state of drifted resources, one PR per owning repo. Returns a plan unless confirm=true.",
  input: {
    resource_ids: z.array(z.string()).min(1),
    org_id: z.string().optional(),
    confirm: z.boolean().optional(),
  },
  scope: "operate",
  captain: "iac",
  confirm: true,
  annotations: { openWorldHint: true },
  cli: { command: "iac fix-drift", positionals: ["resource_ids"], summary: "Fix drift via PR (--yes)" },
  handler: async (args, ctx) => {
    if (!args.confirm) {
      return plan(
        `Fix drift for ${args.resource_ids.length} resource(s)`,
        ["Resolve the owning repo and module of each resource from the map", "Open one PR per repo updating the Terraform to match live state", "Nothing is applied"],
        { resource_ids: args.resource_ids },
      );
    }
    const result = await ctx.client().post("/api/iac/fix-drift", { resource_ids: args.resource_ids, org_id: args.org_id });
    return { executed: true, ...result };
  },
});
