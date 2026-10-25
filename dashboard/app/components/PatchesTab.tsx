"use client";
// The Patches tab: the findings by workload and repository, with exposure, severity, KEV and
// EPSS, the fix version, the verdict and the state; a filter for exposed, exploited and
// waiting for merge; the feeds' last run and the inventory's coverage. The "Needs you" view
// is the inbox feed for this tab. docs/patching.md.
import axios from "axios";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import React, { useCallback, useEffect, useMemo, useState } from "react";

import InboxFeed from "./InboxFeed";
import { BTN_OUTLINE_SM, relativeTime } from "./honeypotFormat";
import { useShell } from "../lib/shell";

type Workload = { node_id: string; node_type: string; name: string; exposed?: boolean; via?: string };
type Finding = {
  key: string; id: string; advisory_id: string; node_id: string; node_type: string; node_name: string; package: string; version?: string | null;
  fixed_version?: string | null; fixed?: string[]; release?: string | null; repo?: string | null; path?: string | null; evidence?: string; relation?: string;
  workloads: Workload[]; exposed: boolean; blockers: string[]; note?: string | null; cvss?: number | null; severity?: string | null; kev: boolean;
  epss?: number | null; summary?: string; state: string; verdict: string; verdict_reasons?: string[]; first_seen?: number; pr?: { url?: string; number?: number } | null;
};
type SourceStatus = { cursor?: unknown; last_success?: string | null; updated_at?: number | null };
type Payload = {
  summary: Record<string, number> & { by_repo?: Record<string, number> };
  findings: Finding[];
  feed: { advisories: number; last_run?: string | null; sources: Record<string, SourceStatus>; report?: { sources?: Record<string, { error?: string; fetched?: number; stored?: number }> } | null };
  tick?: { at?: number; matched?: number; advisories?: number; coverage?: { line?: string } } | null;
  coverage?: { line?: string; images?: number; images_resolved?: number } | null;
};

const SEVERITY: Record<string, string> = { critical: "bg-red-100 text-red-800", high: "bg-orange-100 text-orange-800", medium: "bg-amber-100 text-amber-800", low: "bg-gray-100 text-gray-600" };
const STATE: Record<string, string> = { open: "text-gray-700", approved: "text-blue-700", pr_open: "text-purple-700", merged: "text-purple-700", deployed: "text-green-700", ignored: "text-gray-400", closed: "text-gray-400" };
const STATE_LABEL: Record<string, string> = { open: "open", approved: "approved", pr_open: "pull request open", merged: "merged, not yet running", deployed: "deployed", ignored: "ignored", closed: "closed" };
type Filter = "all" | "exposed" | "exploited" | "waiting";

const advisoryHref = (id: string) => (id.startsWith("CVE-") ? `https://nvd.nist.gov/vuln/detail/${id}` : id.startsWith("GHSA-") ? `https://github.com/advisories/${id}` : `https://osv.dev/vulnerability/${id}`);

function Tile({ label, value, tone }: { label: string; value: number | string; tone?: string }) {
  return (
    <div className="rounded-lg border bg-white px-4 py-3">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`mt-1 text-2xl font-bold ${tone || "text-gray-900"}`}>{value}</div>
    </div>
  );
}

function Row({ f, readOnly, onChanged }: { f: Finding; readOnly: boolean; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const act = async (path: string, body: Record<string, unknown>) => {
    setBusy(true);
    try {
      await axios.post(path, body);
      window.dispatchEvent(new Event("beamreach:inbox-updated"));
      onChanged();
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <tr className="align-top hover:bg-gray-50 cursor-pointer" onClick={() => setOpen((v) => !v)} data-finding={f.key}>
        <td className="px-3 py-2">
          <span className={`inline-flex rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${SEVERITY[f.severity || ""] || SEVERITY.low}`}>{f.severity || "n/a"}</span>
          <div className="mt-1 text-[11px] text-gray-500">{f.cvss != null ? `CVSS ${f.cvss}` : ""}{f.kev ? " · KEV" : ""}{f.epss != null ? ` · EPSS ${(f.epss * 100).toFixed(0)}%` : ""}</div>
        </td>
        <td className="px-3 py-2"><a href={advisoryHref(f.advisory_id)} target="_blank" rel="noopener noreferrer" className="font-mono text-xs text-blue-700 hover:underline" onClick={(e) => e.stopPropagation()}>{f.advisory_id}</a>
          <div className="text-xs text-gray-600 truncate max-w-[16rem]" title={f.summary}>{f.summary}</div></td>
        <td className="px-3 py-2 text-xs"><span className="font-medium text-gray-900">{f.package}</span>{f.version ? <span className="text-gray-500"> {f.version}</span> : <span className="text-gray-400"> (unpinned)</span>}
          {f.fixed_version ? <div className="text-green-700">→ {f.fixed_version}</div> : <div className="text-gray-400">no fix yet</div>}</td>
        <td className="px-3 py-2 text-xs">
          {f.workloads.length ? f.workloads.map((w) => (
            <div key={w.node_id}>{w.name}{w.exposed ? <span className="ml-1 rounded bg-red-50 px-1 text-[10px] font-semibold text-red-700">exposed</span> : null}</div>
          )) : <span className="text-gray-400">no workload on the map</span>}
          {f.repo && <div className="text-gray-500">{f.repo}{f.path ? ` · ${f.path}` : ""}</div>}
        </td>
        <td className="px-3 py-2 text-xs">
          <div className={`font-medium ${STATE[f.state] || ""}`}>{STATE_LABEL[f.state] || f.state}</div>
          <div className="text-gray-500">{f.verdict === "pr" ? "pull request without a click" : "waits for a person"}</div>
          {f.pr?.url && <a href={f.pr.url} target="_blank" rel="noopener noreferrer" className="text-blue-700 hover:underline" onClick={(e) => e.stopPropagation()}>#{f.pr.number}</a>}
        </td>
        <td className="px-3 py-2 text-right text-xs text-gray-400" title={f.first_seen ? new Date(f.first_seen).toISOString() : ""}>{f.first_seen ? relativeTime(f.first_seen) : ""}</td>
      </tr>
      {open && (
        <tr className="bg-gray-50">
          <td colSpan={6} className="px-4 py-3 text-xs text-gray-700">
            <div className="grid gap-x-8 gap-y-1 sm:grid-cols-2">
              <div><span className="text-gray-500">why: </span>{(f.verdict_reasons || []).join("; ")}</div>
              <div><span className="text-gray-500">evidence: </span>{f.evidence}{f.relation ? ` (${f.relation})` : ""}</div>
              {f.note && <div className="sm:col-span-2"><span className="text-gray-500">note: </span>{f.note}</div>}
              {f.release && <div><span className="text-gray-500">release: </span>{f.release}</div>}
              {f.fixed && f.fixed.length > 1 && <div><span className="text-gray-500">fixed in: </span>{f.fixed.join(", ")}</div>}
              <div className="font-mono text-[11px] text-gray-500 sm:col-span-2 break-all">{f.id}</div>
            </div>
            {!readOnly && (f.state === "open" || f.state === "ignored") && (
              <div className="mt-2 flex gap-2">
                {f.state === "open" && f.verdict !== "pr" && <button className={BTN_OUTLINE_SM} disabled={busy} onClick={(e) => { e.stopPropagation(); void act("/api/patches/findings/approve", { keys: [f.key] }); }}>Approve the pull request</button>}
                {f.state === "open" && <button className={BTN_OUTLINE_SM} disabled={busy} onClick={(e) => { e.stopPropagation(); void act("/api/patches/findings/ignore", { keys: [f.key], days: 30 }); }}>Ignore for 30 days</button>}
                {f.state === "ignored" && <button className={BTN_OUTLINE_SM} disabled={busy} onClick={(e) => { e.stopPropagation(); void act(`/api/patches/findings/${f.key}/reopen`, {}); }}>Reopen</button>}
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

export default function PatchesTab() {
  const { settingsBootstrap, fetchQueueJobs } = useShell();
  const searchParams = useSearchParams();
  const view = searchParams?.get("view") === "needs_you" ? "needs_you" : "findings";
  const repoParam = searchParams?.get("repo") || null;
  const readOnly = settingsBootstrap?.app?.read_only === true;
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [state, setState] = useState<string>("");
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await axios.get<Payload>("/api/patches", { params: state ? { state } : {} });
      setData(res.data);
      setError(null);
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || "The patches could not be loaded");
    }
  }, [state]);
  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), 30000);
    return () => window.clearInterval(id);
  }, [load]);

  const rows = useMemo(() => {
    const all = data?.findings || [];
    return all.filter((f) => (!repoParam || f.repo === repoParam) && (filter === "all" || (filter === "exposed" && f.exposed) || (filter === "exploited" && f.kev) || (filter === "waiting" && (f.state === "pr_open" || f.state === "approved"))));
  }, [data, filter, repoParam]);

  const run = async (path: string, label: string) => {
    try {
      await axios.post(path);
      setMessage(`${label} queued`);
      void fetchQueueJobs();
      window.setTimeout(() => setMessage(null), 4000);
    } catch (e: any) {
      setMessage(e?.response?.data?.detail || `${label} failed`);
    }
  };

  const summary = data?.summary;
  const sources = data?.feed?.sources || {};
  const report = data?.feed?.report?.sources || {};
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Patches</h1>
          <p className="text-sm text-gray-600">Every CVE on the map, which workload and repository it reaches, the version that fixes it, and the pull request on its way.</p>
        </div>
        {!readOnly && (
          <div className="flex gap-2">
            <button className={BTN_OUTLINE_SM} onClick={() => void run("/api/patches/feed/run", "The feeds")}>Run the feeds now</button>
            <button className={BTN_OUTLINE_SM} onClick={() => void run("/api/patches/tick", "The match")}>Match now</button>
          </div>
        )}
      </div>
      {message && <div className="rounded border border-blue-200 bg-blue-50 px-4 py-2 text-sm text-blue-800">{message}</div>}
      {error && <div className="rounded border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}

      <nav className="flex gap-2 md:hidden" aria-label="Patches views">
        <Link href="/workspace?tab=patches" className={`rounded-full px-3 py-1 text-xs font-medium ${view === "findings" ? "bg-blue-50 text-blue-700" : "bg-gray-100 text-gray-600"}`}>Findings</Link>
        <Link href="/workspace?tab=patches&view=needs_you" className={`rounded-full px-3 py-1 text-xs font-medium ${view === "needs_you" ? "bg-blue-50 text-blue-700" : "bg-gray-100 text-gray-600"}`}>Needs you</Link>
      </nav>

      {view === "needs_you" ? (
        <InboxFeed tab="patches" readOnly={readOnly} empty="Nothing needs you here. Findings below every trigger, and those whose fix is not a plain bump, would be listed." />
      ) : (
        <>
          {summary && (
            <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
              <Tile label="Open findings" value={summary.open} />
              <Tile label="On exposed workloads" value={summary.exposed} tone={summary.exposed ? "text-red-700" : undefined} />
              <Tile label="Exploited (KEV)" value={summary.exploited} tone={summary.exploited ? "text-red-700" : undefined} />
              <Tile label="Pull requests open" value={summary.pr_open} tone="text-purple-700" />
              <Tile label="Deployed" value={summary.deployed} tone="text-green-700" />
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {(["all", "exposed", "exploited", "waiting"] as Filter[]).map((f) => (
              <button key={f} onClick={() => setFilter(f)} className={`rounded-full px-3 py-1 font-medium ${filter === f ? "bg-blue-50 text-blue-700" : "bg-gray-100 text-gray-600"}`}>
                {f === "all" ? "All" : f === "exposed" ? "Exposed" : f === "exploited" ? "Exploited" : "Waiting for merge"}
              </button>
            ))}
            <select value={state} onChange={(e) => setState(e.target.value)} className="ml-2 rounded border border-gray-300 bg-white px-2 py-1 text-xs" aria-label="State">
              <option value="">open states</option>
              {["open", "approved", "pr_open", "merged", "deployed", "ignored", "closed"].map((s) => <option key={s} value={s}>{STATE_LABEL[s]}</option>)}
            </select>
            {repoParam && <Link href="/workspace?tab=patches" className="rounded-full bg-amber-50 px-3 py-1 text-amber-800">repository {repoParam} ×</Link>}
            <span className="ml-auto text-gray-500">{data?.coverage?.line || ""}{data?.tick?.at ? ` · matched ${relativeTime(data.tick.at)}` : ""}</span>
          </div>
          <div className="overflow-x-auto rounded-lg border bg-white">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b bg-gray-50 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
                  <th className="px-3 py-2">Severity</th><th className="px-3 py-2">Advisory</th><th className="px-3 py-2">Package → fix</th><th className="px-3 py-2">Workload · repository</th><th className="px-3 py-2">State</th><th className="px-3 py-2 text-right">Since</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {data === null ? (
                  <tr><td colSpan={6} className="px-4 py-8 text-center text-sm text-gray-500">Loading…</td></tr>
                ) : rows.length === 0 ? (
                  <tr><td colSpan={6} className="px-4 py-10 text-center text-sm text-gray-500">{data.feed?.advisories ? "Nothing on the map is affected by what the feeds know." : "The feeds have not run yet. They run every hour; \"Run the feeds now\" starts them."}</td></tr>
                ) : rows.map((f) => <Row key={f.key} f={f} readOnly={readOnly} onChanged={() => void load()} />)}
              </tbody>
            </table>
          </div>
          <section className="rounded-lg border bg-white px-4 py-3 text-xs text-gray-700">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-semibold text-gray-900">The feeds</span>
              <span className="text-gray-500">{data?.feed?.advisories ?? 0} advisories stored{data?.feed?.last_run ? ` · last run ${relativeTime(data.feed.last_run)}` : ""}</span>
            </div>
            <ul className="mt-2 grid gap-x-6 gap-y-1 sm:grid-cols-2 lg:grid-cols-5">
              {["osv", "cvelist", "kev", "epss", "ubuntu", "debian", "alpine", "alas", "packagist", "npm"].map((name) => {
                const s = sources[name];
                const r = report[name];
                return (
                  <li key={name} className="flex items-center justify-between gap-2">
                    <span className={r?.error ? "text-red-700" : "text-gray-700"}>{name}</span>
                    <span className="text-gray-400" title={r?.error || ""}>{r?.error ? "failed" : s?.last_success ? relativeTime(s.last_success) : "not yet"}</span>
                  </li>
                );
              })}
            </ul>
          </section>
        </>
      )}
    </div>
  );
}
