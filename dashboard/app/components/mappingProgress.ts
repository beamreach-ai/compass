// Where a running map scan stands, from the mapper job's events (captains/mapper):
// how far along, which region, how many resources so far, how long it has run and
// about how long is left. No React here.
import type { QueueJob } from "../lib/shell";

// What the progress needs of a job and its events: the shell's QueueJob and the Maps
// tab's own summary of the mapper job both fit.
export type ScanEvent = { timestamp?: number; message?: string; data?: Record<string, any> | null };
export type ScanJob = { status: string; events?: ScanEvent[]; created_at?: number };
type TimedEvent = ScanEvent & { timestamp: number };

// The scan's phases in order and how far along each one is. The AWS regions are
// 3% to 85%, divided evenly among them; that is most of a scan.
const SCAN_PHASES: Array<{ prefix: string; pct: number; label: string }> = [
  { prefix: "Initializing infra scan", pct: 1, label: "Starting" },
  { prefix: "Preparing mapper components", pct: 1, label: "Starting" },
  { prefix: "Building infra map", pct: 2, label: "Starting" },
  { prefix: "Resolving target AWS account", pct: 2, label: "Finding the account" },
  { prefix: "Resolving AWS regions", pct: 3, label: "Listing regions" },
  { prefix: "Collecting AWS infrastructure inventory", pct: 3, label: "Reading AWS" },
  { prefix: "AWS inventory collection complete", pct: 85, label: "AWS read" },
  { prefix: "Collecting GitHub ownership facts", pct: 88, label: "Reading GitHub" },
  { prefix: "GitHub ownership collection complete", pct: 91, label: "GitHub read" },
  { prefix: "Collecting Terraform state facts", pct: 93, label: "Reading Terraform state" },
  { prefix: "Terraform state collection complete", pct: 95, label: "Terraform state read" },
  { prefix: "Deriving ownership correspondence", pct: 97, label: "Matching resources to code" },
  { prefix: "Computing IAC coverage and drift", pct: 98, label: "Computing coverage and drift" },
  { prefix: "Writing infra map graph", pct: 99, label: "Writing the map" },
  { prefix: "Infra map graph write complete", pct: 99, label: "Writing the map" },
  { prefix: "Infra scan completed", pct: 100, label: "Done" },
];
const REGIONS_START = 3;
const REGIONS_END = 85;
const REGION_RE = /^Scanning region (\S+) \((\d+)\/(\d+)\)$/;

export type MappingProgress = {
  pct: number;
  phase: string;
  region: string | null;
  regionIndex: number | null;
  regionTotal: number | null;
  resources: number | null;
  elapsedMs: number;
  remainingMs: number | null; // null until there is enough to estimate from
};

type RegionStep = { region: string; index: number; total: number; resources: number | null; at: number };

// DynamoDB numbers can arrive as numbers or as numeric strings
const asCount = (value: unknown): number | null => {
  const n = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : NaN;
  return Number.isFinite(n) ? n : null;
};

function regionStep(event: TimedEvent): RegionStep | null {
  const match = event.message?.match(REGION_RE);
  if (!match) return null;
  const data = (event.data || {}) as { resources?: unknown };
  return {
    region: match[1],
    index: parseInt(match[2], 10),
    total: Math.max(parseInt(match[3], 10), 1),
    resources: asCount(data.resources),
    at: event.timestamp,
  };
}

function latestCount(events: TimedEvent[]): number | null {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const data = (events[i].data || {}) as { resources?: unknown; node_count?: unknown };
    const resources = asCount(data.resources);
    if (resources !== null) return resources;
    const nodes = asCount(data.node_count);
    if (nodes !== null && events[i].message?.startsWith("AWS inventory collection complete")) return nodes;
  }
  return null;
}

// How long the last completed scan took, for an estimate before this one has a pace.
export function lastScanDurationMs(jobs: QueueJob[], exceptJobId?: string): number | null {
  const done = jobs
    .filter((j) => j.captain === "mapper" && j.command === "run" && j.status === "completed" && j.job_id !== exceptJobId && j.events.length > 1)
    .sort((a, b) => b.updated_at - a.updated_at)[0];
  if (!done) return null;
  const start = done.events[0].timestamp;
  const end = done.events[done.events.length - 1].timestamp;
  return end > start ? end - start : null;
}

export function mappingProgress(job: ScanJob, now: number, previousMs: number | null = null): MappingProgress {
  const events = (job.events || []).filter((e): e is TimedEvent => typeof e.timestamp === "number").sort((a, b) => a.timestamp - b.timestamp);
  const started = events.length ? events[0].timestamp : job.created_at || now;
  const elapsedMs = Math.max(0, now - started);
  const latest = events[events.length - 1];
  const steps = events.map(regionStep).filter((s): s is RegionStep => s !== null);
  const step = latest ? regionStep(latest) : null;

  let pct = 1;
  let phase = job.status === "queued" ? "Waiting for the daemon" : "Starting";
  if (step) {
    pct = REGIONS_START + ((step.index - 1) / step.total) * (REGIONS_END - REGIONS_START);
    phase = "Reading AWS";
  } else if (latest) {
    const known = SCAN_PHASES.find((p) => latest.message?.startsWith(p.prefix));
    if (known) {
      pct = known.pct;
      phase = known.label;
    }
  }

  // The estimate: from the pace of the regions done so far when there are any (most of a
  // scan is regions, and they take about the same time), else from the last scan's length,
  // else from the share done once there is enough of it.
  let remainingMs: number | null = null;
  const first = steps[0];
  const done = step ? step.index - 1 : steps.length ? steps[steps.length - 1].total : 0;
  if (first && done >= 1) {
    const total = steps[steps.length - 1].total;
    const regionsEnd = step ? now : (events.find((e) => e.message?.startsWith("AWS inventory collection complete"))?.timestamp ?? now);
    const perRegion = (regionsEnd - first.at) / done;
    const regionsLeft = step ? Math.max(0, total - step.index + 0.5) : 0;
    // what follows the regions (GitHub, Terraform state, writing the map) is about a fifth of a scan
    const after = step ? perRegion * total * 0.2 : Math.max(0, perRegion * total * 0.2 - (now - regionsEnd));
    remainingMs = perRegion * regionsLeft + after;
  } else if (previousMs && previousMs > elapsedMs) {
    remainingMs = previousMs - elapsedMs;
  } else if (pct >= 10) {
    remainingMs = (elapsedMs * (100 - pct)) / pct;
  }

  return {
    pct: Math.min(100, Math.max(1, Math.round(pct))),
    phase,
    region: step?.region ?? null,
    regionIndex: step?.index ?? null,
    regionTotal: step?.total ?? (steps.length ? steps[steps.length - 1].total : null),
    resources: latestCount(events),
    elapsedMs,
    remainingMs: remainingMs === null ? null : Math.max(0, remainingMs),
  };
}

export function formatDuration(ms: number): string {
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `${hours} h ${minutes % 60} min`;
}

// "Region 7 of 17 (eu-west-1) · 1,234 resources found · 4 min elapsed · about 6 min left"
export function progressDetails(p: MappingProgress): string[] {
  const parts: string[] = [];
  if (p.regionIndex && p.regionTotal) parts.push(`Region ${p.regionIndex} of ${p.regionTotal}${p.region ? ` (${p.region})` : ""}`);
  else if (p.regionTotal) parts.push(`${p.regionTotal} regions read`);
  if (p.resources !== null) parts.push(`${p.resources.toLocaleString()} resources found`);
  parts.push(`${formatDuration(p.elapsedMs)} elapsed`);
  if (p.remainingMs !== null) parts.push(p.remainingMs < 60_000 ? "less than a minute left" : `about ${formatDuration(p.remainingMs)} left`);
  return parts;
}
