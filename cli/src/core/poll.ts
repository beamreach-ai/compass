import type { CompassClient } from "./client.js";

export interface JobSummary {
  job_id: string;
  captain: string;
  command: string;
  status: string;
  error?: string | null;
  result_summary?: Record<string, unknown> | null;
  events?: Array<{ timestamp: number; message: string; status?: string | null }>;
  created_at?: number;
  updated_at?: number;
}

const TERMINAL = new Set(["completed", "failed", "error", "cancelled", "skipped"]);

export function isTerminal(status: string | undefined | null): boolean {
  return TERMINAL.has(String(status || "").toLowerCase());
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export interface WaitOptions {
  timeoutMs?: number;
  intervalMs?: number;
  onProgress?: (job: JobSummary) => void;
}

/** Poll a job until it reaches a terminal state. */
export async function waitForJob(client: CompassClient, jobId: string, options: WaitOptions = {}): Promise<JobSummary> {
  const deadline = Date.now() + (options.timeoutMs ?? 30 * 60_000);
  let interval = options.intervalMs ?? 3000;
  let lastEvent = -1;
  while (true) {
    const job = await client.get<JobSummary>(`/api/jobs/${encodeURIComponent(jobId)}`);
    const events = job.events || [];
    if (options.onProgress && events.length - 1 > lastEvent) {
      lastEvent = events.length - 1;
      options.onProgress(job);
    }
    if (isTerminal(job.status)) return job;
    if (Date.now() > deadline) return job;
    await sleep(interval);
    interval = Math.min(interval * 1.3, 15_000);
  }
}
