"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import PRDrawer from "./PRDrawer";
import { BTN_OUTLINE_SM, ReadOnlyContext, relativeTime } from "./honeypotFormat";
import { PR_VIEWS, asPRView, prViewHref } from "./prViews";
import { TYPE_LABEL, prId, type PRRecord } from "./prTypes";

const STATE_BADGE: Record<string, string> = {
  open: "bg-green-100 text-green-700",
  merged: "bg-purple-100 text-purple-700",
  closed: "bg-gray-100 text-gray-600",
};

const JOB_BADGE: Record<string, string> = {
  completed: "bg-green-100 text-green-700",
  failed: "bg-red-100 text-red-700",
  running: "bg-blue-100 text-blue-700",
  queued: "bg-gray-100 text-gray-600",
  retrying: "bg-yellow-100 text-yellow-700",
};

export function WatcherBadge({ pr }: { pr: PRRecord }) {
  if (!pr.tracked) return <span className="text-xs text-gray-400">not tracked</span>;
  const status = pr.watch?.status || "watching";
  const pending = Boolean(pr.pending_revision);
  if (pending) return <span className="inline-flex items-center rounded px-2 py-0.5 text-xs font-medium bg-amber-100 text-amber-800">Revision waiting for approval</span>;
  if (status === "needs_human") {
    return (
      <span className="inline-flex flex-col gap-0.5">
        <span className="inline-flex items-center rounded px-2 py-0.5 text-xs font-medium bg-amber-100 text-amber-800 w-fit">Needs you</span>
        {pr.watch?.reason && <span className="text-[11px] text-amber-700">{String(pr.watch.reason).replace(/_/g, " ")}</span>}
      </span>
    );
  }
  const cls = status === "paused" ? "bg-gray-100 text-gray-600" : status === "done" ? "bg-gray-100 text-gray-500" : "bg-blue-50 text-blue-700";
  const label = status === "paused" ? "Paused" : status === "done" ? "Done" : "Watching";
  return <span className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${cls}`}>{label}</span>;
}

function StateBadge({ pr }: { pr: PRRecord }) {
  if (pr.tracked && pr.state) {
    return <span className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${STATE_BADGE[pr.state] ?? "bg-gray-100 text-gray-600"}`}>{pr.state}</span>;
  }
  const status = pr.job_status || "queued";
  return (
    <span className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${JOB_BADGE[status] ?? "bg-gray-100 text-gray-600"}`}>
      {pr.pr_url && status === "completed" ? "PR open" : status}
    </span>
  );
}

function ReviewCell({ pr }: { pr: PRRecord }) {
  const decision = pr.github?.review_decision;
  if (!decision) return <span className="text-gray-400">—</span>;
  const label = decision === "APPROVED" ? "Approved" : decision === "CHANGES_REQUESTED" ? "Changes requested" : "Pending";
  const cls = decision === "APPROVED" ? "text-green-700" : decision === "CHANGES_REQUESTED" ? "text-amber-700" : "text-gray-600";
  return <span className={`text-xs font-medium ${cls}`}>{label}</span>;
}

function ChecksCell({ pr }: { pr: PRRecord }) {
  const checks = pr.github?.checks;
  if (!checks || !checks.total) return <span className="text-gray-400">—</span>;
  const failed = checks.failed || 0;
  const pending = checks.pending || 0;
  if (failed) return <span className="text-xs font-medium text-red-700">{failed} failed</span>;
  if (pending) return <span className="text-xs font-medium text-gray-600">{pending} pending</span>;
  return <span className="text-xs font-medium text-green-700">{checks.total}/{checks.total} ✓</span>;
}

function PRLink({ pr }: { pr: PRRecord }) {
  const label = pr.tracked && pr.number ? `${pr.repo}#${pr.number}` : pr.repo ?? pr.pr_url?.replace("https://github.com/", "") ?? "—";
  return (
    <span className="flex flex-col">
      {pr.pr_url ? (
        <a href={pr.pr_url} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline font-medium" onClick={(e) => e.stopPropagation()}>
          {label}
        </a>
      ) : (
        <span className="text-gray-400 italic">{label}</span>
      )}
      {pr.title && <span className="text-xs text-gray-500 truncate max-w-[28rem]">{pr.title}</span>}
      {!pr.pr_url && pr.job_status === "failed" && pr.error && <span className="text-xs text-red-500">{pr.error.slice(0, 80)}</span>}
    </span>
  );
}

export default function PRsTab() {
  const searchParams = useSearchParams();
  const view = asPRView(searchParams.get("view"));
  const [prs, setPrs] = useState<PRRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [readOnly, setReadOnly] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  const fetchPRs = useCallback(async () => {
    try {
      const res = await fetch("/api/prs?limit=300");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setPrs(await res.json());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load pull requests");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchPRs();
    const id = setInterval(() => void fetchPRs(), 15_000);
    return () => clearInterval(id);
  }, [fetchPRs]);

  useEffect(() => {
    fetch("/api/settings/bootstrap")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setReadOnly(d?.app?.read_only === true))
      .catch(() => undefined);
  }, []);

  const rows = useMemo(() => {
    if (view === "needs_you") return prs.filter((p) => p.tracked && (p.watch?.status === "needs_human" || Boolean(p.pending_revision)));
    if (view === "history") return prs.filter((p) => p.tracked && (p.state === "merged" || p.state === "closed"));
    return prs.filter((p) => !p.tracked || p.state === "open" || !p.state);
  }, [prs, view]);

  const needsYou = prs.filter((p) => p.tracked && (p.watch?.status === "needs_human" || Boolean(p.pending_revision))).length;
  const selectedPr = selected ? prs.find((p) => prId(p) === selected) ?? null : null;

  return (
    <ReadOnlyContext.Provider value={readOnly}>
      <div className="space-y-4">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">Pull requests</h1>
            <p className="text-sm text-gray-600">Every pull request Compass opened, what it must keep doing, and what Compass did about the reviews.</p>
          </div>
          <button onClick={() => void fetchPRs()} className={BTN_OUTLINE_SM}>Refresh</button>
        </div>

        <nav className="flex gap-2 md:hidden" aria-label="Pull request views">
          {PR_VIEWS.map((v) => (
            <a key={v.key} href={prViewHref(v.key)} className={`rounded-full px-3 py-1 text-xs font-medium ${view === v.key ? "bg-blue-50 text-blue-700" : "bg-gray-100 text-gray-600"}`}>
              {v.label}{v.key === "needs_you" && needsYou ? ` (${needsYou})` : ""}
            </a>
          ))}
        </nav>

        {error && <div className="rounded border border-red-200 bg-red-50 text-red-700 px-4 py-2 text-sm">{error}</div>}

        {loading ? (
          <div className="text-sm text-gray-500 py-8 text-center">Loading…</div>
        ) : rows.length === 0 ? (
          <div className="rounded-lg border bg-white px-6 py-12 text-center text-sm text-gray-500">
            {view === "needs_you" ? "Nothing is waiting for you." : view === "history" ? "No merged or closed pull requests yet." : "No pull requests yet. Fix a finding, import unmanaged resources or plant a honeytoken to open one."}
          </div>
        ) : (
          <>
            <div className="hidden md:block rounded-lg border bg-white overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-gray-50 text-left text-xs font-semibold uppercase tracking-wide text-gray-500 border-b">
                    <th className="px-4 py-3">Type</th>
                    <th className="px-4 py-3">Pull request</th>
                    <th className="px-4 py-3">State</th>
                    <th className="px-4 py-3">Review</th>
                    <th className="px-4 py-3">Checks</th>
                    <th className="px-4 py-3">Watcher</th>
                    <th className="px-4 py-3">Revisions</th>
                    <th className="px-4 py-3">Updated</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {rows.map((pr) => {
                    const id = prId(pr);
                    return (
                      <tr
                        key={`${pr.job_id ?? ""}-${pr.pr_url ?? id}`}
                        className={`hover:bg-gray-50 ${pr.tracked ? "cursor-pointer" : ""}`}
                        onClick={() => pr.tracked && id && setSelected(id)}
                      >
                        <td className="px-4 py-3 text-gray-600">{TYPE_LABEL[pr.type] ?? pr.type}</td>
                        <td className="px-4 py-3"><PRLink pr={pr} /></td>
                        <td className="px-4 py-3"><StateBadge pr={pr} /></td>
                        <td className="px-4 py-3"><ReviewCell pr={pr} /></td>
                        <td className="px-4 py-3"><ChecksCell pr={pr} /></td>
                        <td className="px-4 py-3"><WatcherBadge pr={pr} /></td>
                        <td className="px-4 py-3 text-gray-600">{pr.tracked ? `${pr.revision_count ?? pr.counters?.revisions ?? 0}` : "—"}</td>
                        <td className="px-4 py-3 text-gray-400 text-xs">{relativeTime(pr.updated_at ?? pr.created_at ?? null)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <ul className="md:hidden space-y-2">
              {rows.map((pr) => {
                const id = prId(pr);
                return (
                  <li key={`${pr.job_id ?? ""}-${pr.pr_url ?? id}`} className="rounded-lg border bg-white p-3 space-y-2" onClick={() => pr.tracked && id && setSelected(id)}>
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-medium text-gray-500">{TYPE_LABEL[pr.type] ?? pr.type}</span>
                      <StateBadge pr={pr} />
                    </div>
                    <PRLink pr={pr} />
                    <div className="flex items-center justify-between gap-2">
                      <WatcherBadge pr={pr} />
                      <span className="text-xs text-gray-400">{relativeTime(pr.updated_at ?? pr.created_at ?? null)}</span>
                    </div>
                  </li>
                );
              })}
            </ul>
          </>
        )}

        {selectedPr && selectedPr.repo && selectedPr.number && (
          <PRDrawer repo={selectedPr.repo} number={selectedPr.number} onClose={() => setSelected(null)} onChanged={fetchPRs} />
        )}
      </div>
    </ReadOnlyContext.Provider>
  );
}
