export type GuardCheck = { name: string; ok: boolean; detail?: string | null };

export type Revision = {
  at: number;
  job_id?: string | null;
  trigger: string;
  outcome: "pushed" | "refused" | "failed" | string;
  reason?: string | null;
  commit_sha?: string | null;
  addressed?: Array<string | number>;
  files?: string[];
  guard?: { ok: boolean; reason?: string | null; checks: GuardCheck[] } | null;
  blockers?: string[];
  risk?: number | null;
  note?: string | null;
  context?: { summary?: string | null; changed_since_pr?: string[]; notes?: string[] } | null;
};

export type Decision = { at: number; decision: string; reason?: string | null; trigger?: string; risk?: number; comments?: number; by?: string | null };

export type PendingRevision = {
  comments: Array<{ id: string | number; path?: string | null; line?: number | null; author?: string | null; kind?: string; body?: string }>;
  files: string[];
  risk: { risk: number; notes: string[] };
  mode: string;
  risk_max: number;
  trigger: string;
  created_at?: number;
};

export type PRRecord = {
  tracked: boolean;
  repo: string | null;
  number?: number;
  url?: string | null;
  pr_url: string | null;
  title?: string | null;
  branch?: string | null;
  base_branch?: string | null;
  type: string;
  state?: "open" | "merged" | "closed" | null;
  origin?: { captain?: string; command?: string; finding_id?: string | null; honeytoken_id?: string | null; rope_id?: string | null; job_id?: string | null; user_id?: string | null } | null;
  intent?: {
    goal?: string;
    action_family?: string;
    done_criteria?: string[];
    patch_summary?: string | null;
    scope?: { files?: string[]; dirs?: string[]; resource_ids?: string[] };
    invariants_text?: string[];
    risk?: number | null;
  } | null;
  github?: { review_decision?: string | null; checks?: { total?: number; failed?: number; pending?: number } | null; mergeable_state?: string | null; head_sha?: string | null } | null;
  watch?: { status: string; reason?: string | null } | null;
  counters?: { revisions?: number; refusals?: number; human_commits?: number } | null;
  revision_count?: number;
  pending_revision?: boolean | PendingRevision | null;
  revisions?: Revision[];
  decisions?: Decision[];
  created_at?: number;
  updated_at?: number;
  // job-derived rows
  job_id?: string;
  job_status?: string | null;
  error?: string | null;
  resource_count?: number | null;
};

export const TYPE_LABEL: Record<string, string> = {
  security_fix: "Security fix",
  finops_fix: "FinOps fix",
  iac_import: "IaC import",
  fix_drift: "Drift fix",
  honeypot_plant: "Honeytoken plant",
  honeypot_decommission: "Honeytoken decommission",
  honeypot_responder: "Responder role",
};

export const prId = (pr: PRRecord): string | null => (pr.repo && pr.number ? `${pr.repo}#${pr.number}` : null);
export const prPath = (repo: string, number: number) => `/api/prs/${repo}/${number}`;
