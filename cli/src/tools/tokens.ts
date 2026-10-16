import { z } from "zod";
import { defineTool } from "../core/registry.js";

export const tokensTool = defineTool({
  name: "compass_tokens",
  description:
    "API tokens for this workspace (admin). list; create(name, scope read|operate|admin) returns the plaintext once; revoke(token_id). Tokens are what the CLI and agents authenticate with.",
  input: {
    action: z.enum(["list", "create", "revoke"]),
    name: z.string().optional().describe("Label, e.g. 'ci' or 'jane@laptop' (create)"),
    scope: z.enum(["read", "operate", "admin"]).optional().describe("Default operate (create)"),
    token_id: z.string().optional().describe("Token to revoke"),
  },
  scope: "admin",
  sensitive: true,
  annotations: { destructiveHint: true },
  cli: { command: "tokens", positionals: ["action", "token_id"], summary: "list | create --name --scope | revoke <token_id>" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    switch (args.action) {
      case "list":
        return (await client.get("/api/auth/tokens")).items;
      case "create": {
        if (!args.name) throw new Error("name is required");
        const created = await client.post("/api/auth/tokens", { name: args.name, scope: args.scope || "operate" });
        ctx.log("Store this token now; Compass will not show it again.");
        return created;
      }
      case "revoke":
        if (!args.token_id) throw new Error("token_id is required");
        return client.delete(`/api/auth/tokens/${encodeURIComponent(args.token_id)}`);
    }
  },
});
