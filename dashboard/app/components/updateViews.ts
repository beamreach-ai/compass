// What the update banner and the Compass card derive from /api/updates and the
// bootstrap's `update` (compass_core.updates). No React here.
import type { UpdateState } from "../lib/shell";

export type UpdateResult = {
  status: "pulling" | "waiting" | "applying" | "updated" | "failed";
  from: string;
  to: string;
  mode: "manual" | "auto";
  error?: string | null;
  at: number;
};

export type UpdatesListing = {
  update: UpdateState;
  policy: { auto: "notify" | "patch" | "minor" | "off"; window?: string | null; source: "default" | "env" | "config" };
  request?: { target: string; requested_at: number; by?: string | null; mode: string; force?: boolean } | null;
  pending?: { from: string; to: string; started_at: number } | null;
  result?: UpdateResult | null;
  rollback?: { to: string; until: number } | null;
  jobs?: { version: string; active_jobs: number; at: number } | null;
  updater: { alive: boolean; at?: number | null; version?: string | null };
  commands: string[];
};

export const POLICY_LABELS: Record<UpdatesListing["policy"]["auto"], string> = {
  notify: "Tell me; apply nothing without a click",
  patch: "Apply patch releases on my updater",
  minor: "Apply patch and minor releases on my updater",
  off: "Do not check",
};

const parts = (v: string): number[] => (v.match(/^\d+(\.\d+)*/)?.[0] || "").split(".").map((n) => Number(n) || 0);

// The dashboard and the API differ by more than a patch: the same major and minor, or not.
export const versionsDiffer = (a: string | null | undefined, b: string | null | undefined): boolean => {
  if (!a || !b || a === "dev" || b === "dev") return false;
  const [x, y] = [parts(a), parts(b)];
  return x[0] !== y[0] || x[1] !== y[1];
};

export const DISMISS_KEY = "compass:update-dismissed";

export function dismissedVersion(): string | null {
  try {
    return window.localStorage.getItem(DISMISS_KEY);
  } catch {
    return null;
  }
}

export function dismissVersion(version: string): void {
  try {
    window.localStorage.setItem(DISMISS_KEY, version);
  } catch {
    // private window or blocked storage: the banner simply comes back
  }
}

// One line for where an update request stands, or null when nothing is in flight.
export function phaseText(listing: UpdatesListing | null): string | null {
  if (!listing) return null;
  if (listing.pending) return `Updating to ${listing.pending.to}: waiting for Compass to come back…`;
  const r = listing.result;
  if (listing.request && (!r || r.at < listing.request.requested_at)) return `Update to ${listing.request.target} requested; the updater picks it up within 30 seconds.`;
  if (!r) return null;
  switch (r.status) {
    case "waiting":
      return r.error || `Waiting for running jobs to finish before updating to ${r.to}.`;
    case "pulling":
      return `Pulling the images for ${r.to}…`;
    case "applying":
      return `Recreating the services on ${r.to}…`;
    default:
      return null;
  }
}

export const relativeTime = (seconds: number | null | undefined): string => {
  if (!seconds) return "never";
  const ago = Math.max(0, Date.now() / 1000 - seconds);
  if (ago < 90) return "just now";
  if (ago < 5400) return `${Math.round(ago / 60)} minutes ago`;
  if (ago < 129600) return `${Math.round(ago / 3600)} hours ago`;
  return `${Math.round(ago / 86400)} days ago`;
};
