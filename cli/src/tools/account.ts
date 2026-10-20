import { z } from "zod";
import { defineTool } from "../core/registry.js";

const accountInput = {
  action: z.enum(["get", "connect", "template", "verify", "test"]).describe(
    "get: what is connected; connect: print the `compass connect` command to run with the profile (or, with mode assume_role, record a role made by hand); template: write the roles as CloudFormation/Terraform for an account out of reach; verify: assume the roles as the runtime identity and simulate the read policy; test: check a hand-made role without saving",
  ),
  mode: z.enum(["assume_role"]).optional().describe("assume_role: record a read role made outside `connect` (its template, or by hand)"),
  account_id: z.string().length(12).optional().describe("Target account id"),
  role_name: z.string().optional().describe("The read role to use (assume_role); default svc-inventory-reader-<install>"),
  act_role_name: z.string().optional().describe("The act role, if one exists (assume_role)"),
  external_id: z.string().optional().describe("External id in the trust policy; defaults to the one Compass recorded (aws.external_id)"),
  base_identity: z.string().optional().describe("template: the identity Compass runs with (aws.runtime_arn from get, or the machine's role)"),
  region: z.string().optional().describe("Target region"),
  profile: z.string().optional().describe("connect: the AWS profile in ~/.aws to run it with (printed into the command)"),
  state_bucket: z.string().optional().describe("connect: the Terraform state bucket the mapper may read"),
  account_name: z.string().optional().describe("Friendly name shown in the dashboard"),
};

function rolePayload(args: z.infer<z.ZodObject<typeof accountInput>>) {
  if (!args.account_id) throw new Error("account_id is required for assume_role");
  return {
    mode: "assume_role",
    region: args.region,
    account_name: args.account_name,
    assume_role: { account_id: args.account_id, role_name: args.role_name || "", external_id: args.external_id, act_role_name: args.act_role_name },
  };
}

export const accountTool = defineTool({
  name: "compass_account",
  description:
    "Target AWS account Compass reads. Compass creates its own roles once with `compass connect`, run in the install directory with the operator's profile: connect prints that command. get = what is connected (roles, runtime identity, last verification); verify = check the roles as the runtime identity; template = the roles as CloudFormation/Terraform for an account out of reach; test/connect with mode assume_role = a role made by hand.",
  input: accountInput,
  scope: "admin",
  annotations: { readOnlyHint: false, idempotentHint: true },
  cli: { command: "account", positionals: ["action"], summary: "get | connect | template | verify | test the target AWS account" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    if (args.action === "get") {
      const [target, bootstrap] = await Promise.all([client.get("/api/target"), client.get("/api/settings/bootstrap")]);
      return { target, aws: bootstrap?.aws, infra_scan: bootstrap?.infra_scan };
    }
    if (args.action === "verify") return client.post("/api/settings/aws/verify", {});
    if (args.action === "template") {
      const bootstrap = await client.get("/api/settings/bootstrap");
      const base = args.base_identity || bootstrap?.aws?.runtime_arn;
      const account = args.account_id || bootstrap?.aws?.account_id;
      if (!account || !base) throw new Error("template needs account_id and base_identity (aws.runtime_arn from `compass account get`)");
      return {
        run_in_the_install_directory: `docker compose run --rm compass connect template account=${account} region=${args.region || bootstrap?.aws?.region || "us-east-1"} base_identity=${base}`,
        note: "writes config/connect/<account>/compass-roles.yaml and main.tf; apply either in that account, then `compass account verify`",
      };
    }
    if (args.action === "test") return client.post("/api/settings/aws/test", rolePayload(args));
    if (args.mode === "assume_role") {
      const result = await client.post("/api/settings/aws", rolePayload(args));
      ctx.log("Read role recorded. Compass queued the first infra map.");
      return result;
    }
    const bootstrap = await client.get("/api/settings/bootstrap");
    const account = args.account_id || bootstrap?.aws?.account_id || "<account id>";
    const region = args.region || bootstrap?.aws?.region || "us-east-1";
    const extra = args.state_bucket ? ` state_bucket=${args.state_bucket}` : "";
    return {
      connected: Boolean(bootstrap?.aws?.connected),
      run_in_the_install_directory: `docker compose run --rm -v ~/.aws:/root/.aws:ro -e AWS_PROFILE=${args.profile || "<profile>"} compass connect account=${account} region=${region}${extra}`,
      then: "docker compose up -d",
      note: "the one command that uses the profile: it creates two IAM roles, an alert queue and a runtime user, verifies them and records them in config/local.json",
    };
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
