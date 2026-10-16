import { z } from "zod";
import { defineTool, plan } from "../core/registry.js";

/** Settings → Captains from the shell: what the install carries, on or off, and each one's config. */
export const captainsTool = defineTool({
  name: "compass_captains",
  description:
    "The captains this Compass carries (compass_core.captains): list = every captain with tier, state and why it is off; catalog = what can be installed (query to search), each with its status here and the COMPASS_EXTENSIONS line to add; enable / disable = turn one on or off for the workspace (with_dependencies / cascade when the API asks); config_get / config_set = a captain's configuration section (secrets come back masked; send the mask back to keep one).",
  input: {
    action: z.enum(["list", "catalog", "enable", "disable", "config_get", "config_set"]),
    query: z.string().optional().describe("catalog: words to search for"),
    captain: z.string().optional().describe("enable | disable | config_*: the captain's name (compass_captains list)"),
    with_dependencies: z.boolean().optional().describe("enable: also turn on the captains it needs"),
    cascade: z.boolean().optional().describe("disable: also turn off the captains that need it"),
    config: z.object({}).passthrough().optional().describe("config_set: the fields to change, as an object (null removes a field)"),
    replace: z.boolean().optional().describe("config_set: replace the whole section instead of merging"),
  },
  scope: "admin",
  annotations: { idempotentHint: true },
  cli: { command: "captains", positionals: ["action", "captain"], summary: "list | catalog [--query …] | enable <name> | disable <name> | config_get <name> | config_set <name> --config '{...}'" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    if (args.action === "list") return client.get("/api/captains");
    if (args.action === "catalog") return client.get("/api/captains/catalog", { q: args.query });
    if (!args.captain) throw new Error("captain is required");
    const name = encodeURIComponent(args.captain);
    switch (args.action) {
      case "enable":
        return client.post(`/api/captains/${name}/enable`, { with_dependencies: args.with_dependencies === true });
      case "disable":
        return client.post(`/api/captains/${name}/disable`, { cascade: args.cascade === true });
      case "config_get":
        return client.get(`/api/captains/${name}/config`);
      case "config_set":
        if (!args.config) throw new Error("config is required");
        return client.request("PUT", `/api/captains/${name}/config`, { body: args.config, params: { replace: args.replace === true ? "true" : undefined } });
    }
  },
  render: (result) => {
    const r = result as any;
    if (Array.isArray(r?.entries)) {
      if (r.enabled === false) return "The captain catalog is turned off for this install (COMPASS_CATALOG_URL=off).";
      const lines = [`Catalog ${r.source}${r.error ? " (fetch failed; the copy shipped with this Compass)" : ""}`, "name          tier        version   status"];
      for (const e of r.entries) {
        lines.push(`${String(e.name).padEnd(13)} ${String(e.tier).padEnd(11)} ${String(e.version).padEnd(9)} ${e.status}${e.licensed ? "" : " (not licensed)"}`);
        if (e.env_line && (e.status === "install" || e.status === "update")) lines.push(`  ${e.env_line}`);
      }
      return lines.join("\n");
    }
    if (Array.isArray(r?.captains)) {
      const rows = r.captains.filter((c: any) => !c.core);
      const lines = [`Compass ${r.core_version}${r.licence?.plan ? `  plan ${r.licence.plan}` : ""}`, "name          tier        state  detail"];
      for (const c of rows) {
        const state = c.active ? "on" : "off";
        const detail = c.active ? "" : c.reason === "dependency_off" ? `needs ${c.blocked_by.join(", ")}` : c.reason || "";
        lines.push(`${String(c.name).padEnd(13)} ${String(c.tier).padEnd(11)} ${state.padEnd(6)} ${detail}`);
      }
      return lines.join("\n");
    }
    if (r?.captain?.name) {
      const c = r.captain;
      return `${c.label}: ${c.active ? "on" : `off (${c.reason || "disabled"})`}${Array.isArray(r.changed) && r.changed.length > 1 ? `; also changed: ${r.changed.filter((n: string) => n !== c.name).join(", ")}` : ""}`;
    }
    return JSON.stringify(r, null, 2);
  },
});

/** Install, update or remove a captain from the catalog, and restart Compass to apply (spec §10, M2–M4). */
export const extensionsTool = defineTool({
  name: "compass_extensions",
  description:
    "Change which captains are installed in this Compass. install = install a catalog captain, or update it to the catalog's version (compass_captains catalog shows what is available); uninstall = remove one installed from the catalog (its data and settings stay); restart = restart Compass so the changes load; state = what is installed, requested and waiting for a restart. install, uninstall and restart return a plan unless confirm=true: installing runs the captain's code in this Compass.",
  input: {
    action: z.enum(["state", "install", "uninstall", "restart"]),
    captain: z.string().optional().describe("install | uninstall: the captain's name"),
    confirm: z.boolean().optional(),
  },
  scope: "admin",
  confirm: true,
  cli: { command: "extensions", positionals: ["action", "captain"], summary: "state | install <name> | uninstall <name> | restart (--yes to apply)" },
  handler: async (args, ctx) => {
    const client = ctx.client();
    if (args.action === "state") return client.get("/api/captains/extensions");
    if (args.action === "restart") {
      if (!args.confirm) return plan("Restart Compass to load the changed captains", ["POST /api/captains/restart: the API and the daemon exit and come back; the entrypoint installs the new set on the way up"]);
      return client.post("/api/captains/restart", {});
    }
    if (!args.captain) throw new Error("captain is required");
    if (args.action === "install") {
      const listing = await client.get("/api/captains/catalog", { q: args.captain });
      const entry = (listing?.entries || []).find((e: any) => e.name === args.captain);
      if (!entry) throw new Error(`${args.captain} is not in the catalog`);
      if (!args.confirm) {
        return plan(
          `${entry.status === "update" ? "Update" : "Install"} ${entry.label} ${entry.version} (${entry.tier}) from the catalog`,
          [`pip install ${entry.pip ?? "(no public package)"}`, "takes effect at the next restart (compass extensions restart --yes)"],
          { source: entry.source ?? null, author: entry.author ?? null, status: entry.status, licensed: entry.licensed },
          entry.licensed ? undefined : ["This workspace's licence does not include it: it will install but stay off."],
        );
      }
      return client.post("/api/captains/install", { name: args.captain });
    }
    if (!args.confirm) return plan(`Uninstall ${args.captain}`, ["removed at the next restart; its data and settings stay"]);
    return client.post("/api/captains/uninstall", { name: args.captain });
  },
});
