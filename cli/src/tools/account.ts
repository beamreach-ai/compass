import { z } from "zod";
import { defineTool } from "../core/registry.js";

const accountInput = {
  action: z.enum(["get", "test", "connect"]).describe("get: current target; test: validate credentials without saving; connect: validate and save, which also starts the first infra map and audits"),
  mode: z.enum(["local_iam", "assume_role", "access_key"]).optional().describe("local_iam = the account Compass runs in; assume_role = cross-account role; access_key = static key (fallback)"),
  account_id: z.string().length(12).optional().describe("Target account id (assume_role)"),
  role_name: z.string().optional().describe("Role to assume; default BeamreachCompassRole"),
  external_id: z.string().optional().describe("External id; defaults to the one Compass generated (see compass_settings get)"),
  access_key_id: z.string().optional(),
  secret_access_key: z.string().optional(),
  session_token: z.string().optional(),
  region: z.string().optional().describe("Default region for scans"),
  account_name: z.string().optional().describe("Friendly name shown in the dashboard"),
};

function payloadFrom(args: z.infer<z.ZodObject<typeof accountInput>>) {
  const mode = args.mode || (args.account_id ? "assume_role" : args.access_key_id ? "access_key" : "local_iam");
  const payload: Record<string, unknown> = { mode, region: args.region, account_name: args.account_name };
  if (mode === "assume_role") {
    if (!args.account_id) throw new Error("account_id is required for assume_role");
    payload.assume_role = { account_id: args.account_id, role_name: args.role_name || "BeamreachCompassRole", external_id: args.external_id };
  }
  if (mode === "access_key") {
    if (!args.access_key_id || !args.secret_access_key) throw new Error("access_key_id and secret_access_key are required for access_key mode");
    payload.access_key = { access_key_id: args.access_key_id, secret_access_key: args.secret_access_key, session_token: args.session_token };
  }
  return payload;
}

export const accountTool = defineTool({
  name: "compass_account",
  description:
    "Target AWS account Compass scans. get = what is configured; test = STS check of a proposed configuration; connect = save it (admin). For a cross-account target, deploy the role first (compass_account_role_template) and pass account_id; the external id defaults to the one Compass generated.",
  input: accountInput,
  scope: "admin",
  annotations: { readOnlyHint: false, idempotentHint: true },
  cli: { command: "account", positionals: ["action"], summary: "get | test | connect the target AWS account" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    if (args.action === "get") {
      const [target, bootstrap] = await Promise.all([client.get("/api/target"), client.get("/api/settings/bootstrap")]);
      return { target, aws: bootstrap?.aws, infra_scan: bootstrap?.infra_scan };
    }
    const payload = payloadFrom(args);
    if (args.action === "test") return client.post("/api/settings/aws/test", payload);
    const result = await client.post("/api/settings/aws", payload);
    ctx.log("AWS target saved. Compass queued the first infra map and audits.");
    return result;
  },
});

export const githubTool = defineTool({
  name: "compass_github",
  description:
    "GitHub connection used for the infra map and for fix, import, drift and honeypot PRs. status = current connection; connect = with github_token: connects immediately with a GitHub token (repo scope; fine-grained PATs work) and starts the infra map, no browser needed; without token: returns the GitHub App install URL the user must open in a browser, then poll status until connected.",
  input: {
    action: z.enum(["status", "connect"]),
    github_token: z.string().optional().describe("GitHub token with repo scope; stored by Compass, never returned"),
    repositories: z.array(z.string()).optional().describe("owner/name repos to scan for secrets and IaC issues (optional)"),
  },
  scope: "admin",
  annotations: { readOnlyHint: false, openWorldHint: true },
  sensitive: true,
  cli: { command: "github", positionals: ["action"], summary: "status | connect [--github-token ghp_… --repositories a/b,c/d] (no token: prints the App link)" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    if (args.action === "connect" && args.github_token) {
      const result = await client.post("/api/integrations/github/token", { token: args.github_token, repositories: args.repositories });
      ctx.log(`GitHub connected as ${result.login}; infra map ${result.infra_scan_job_id ? `queued (${result.infra_scan_job_id})` : "will start once AWS is connected"}.`);
      return result;
    }
    const bootstrap = await client.get("/api/settings/bootstrap");
    const github = bootstrap?.github || {};
    if (args.action === "status") return github;
    if (github.connected) return { connected: true, github, note: "Already connected. Use connect again only to reinstall the App." };
    const connectUrl = client.url("/api/integrations/github/connect");
    return {
      connected: false,
      open_in_browser: connectUrl,
      note: "Open this link in a browser, finish the GitHub App install, then re-check with action=status. The link must be opened with a dashboard session (cookie or Cognito), not with the API token.",
    };
  },
});
