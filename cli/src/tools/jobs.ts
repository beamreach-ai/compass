import { z } from "zod";
import { defineTool } from "../core/registry.js";
import { waitForJob } from "../core/poll.js";

export const jobsTool = defineTool({
  name: "compass_jobs",
  description:
    "Background jobs (scans, maps, triage, fixes, PRs). list(limit, captain, status); get(job_id) with the event timeline; wait(job_id) blocks until it finishes; restart(job_id) re-queues a finished or failed job.",
  input: {
    action: z.enum(["list", "get", "wait", "restart"]),
    job_id: z.string().optional(),
    limit: z.number().int().min(1).max(500).optional().describe("list: default 50"),
    captain: z.string().optional().describe("list: filter, e.g. audit, finops, mapper, triage, engine, iac, honeypot"),
    status: z.string().optional().describe("list: filter, e.g. running, failed, completed"),
    timeout_seconds: z.number().int().min(10).max(7200).optional().describe("wait: default 1800"),
  },
  scope: "read",
  annotations: { readOnlyHint: false, idempotentHint: true },
  cli: { command: "jobs", positionals: ["action", "job_id"], summary: "list | get <id> | wait <id> | restart <id>" },
  exitCode: (result) => (result && !Array.isArray(result) && /^(failed|error|cancelled)$/i.test(String((result as any).status || "")) ? 1 : 0),
  handler: async (args, ctx) => {
    const client = ctx.client();
    switch (args.action) {
      case "list": {
        const jobs = await client.get("/api/jobs", { limit: args.limit ?? 50 });
        return (jobs as any[])
          .filter((j) => (!args.captain || j.captain === args.captain) && (!args.status || String(j.status).toLowerCase() === args.status.toLowerCase()))
          .map(({ events, ...rest }) => ({ ...rest, events_count: events?.length ?? 0 }));
      }
      case "get":
        if (!args.job_id) throw new Error("job_id is required");
        return client.get(`/api/jobs/${encodeURIComponent(args.job_id)}`);
      case "wait":
        if (!args.job_id) throw new Error("job_id is required");
        return waitForJob(client, args.job_id, {
          timeoutMs: (args.timeout_seconds ?? 1800) * 1000,
          onProgress: (job) => {
            const last = job.events?.[job.events.length - 1];
            if (last) ctx.log(`[${job.captain}.${job.command}] ${job.status}: ${last.message}`);
          },
        });
      case "restart":
        if (!args.job_id) throw new Error("job_id is required");
        return client.post(`/api/jobs/${encodeURIComponent(args.job_id)}/restart`);
    }
  },
});
