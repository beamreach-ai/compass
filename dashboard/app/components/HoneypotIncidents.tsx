"use client";

import React, { useCallback, useEffect, useState } from "react";
import { LINK_BTN, exactUtc, relativeTime, tail, useReadOnly, writeProps } from "./honeypotFormat";

/**
 * Incidents: the alerts of one intrusion, grouped. Rendered either as the
 * latest-incident card (Overview, `show="latest"`) or as the full list
 * (History, `show="table"`); both open the same drawer, which shows the
 * timeline (with deltas from the first event), the alerts, the response
 * audits and a Close button.
 */

type Summary = { paragraph: string; bullets: string[]; model?: string; generated_at?: string };

export type Incident = {
  incident_id: string;
  status: "open" | "contained" | "closed" | string;
  honeytoken_ids: string[];
  service_arn?: string | null;
  source_task_id?: string | null;
  source_ip?: string | null;
  alert_count: number;
  repeat_count: number;
  related_incident_id?: string | null;
  last_event_time?: string | null;
  summary?: Summary | null;
  [field: string]: unknown;
};

type Alert = {
  alert_id: string;
  honeytoken_id: string;
  triggered_at?: string;
  event_name?: string;
  service?: string;
  source_ip?: string;
  source_task_id?: string;
  kind?: string;
};

type Audit = {
  audit_id: string;
  action_type: string;
  status: string;
  started_at?: string;
  verify?: { ok?: boolean; new_task?: string; steady_after_s?: number } | null;
  error?: string | null;
};

type Detail = { incident: Incident; alerts: Alert[]; audits: Audit[] };

// In timeline order. Any field present on the incident is shown.
const TIMELINE: Array<[string, string]> = [
  ["first_event_time", "First event"],
  ["first_received_at", "Received"],
  ["first_alert_sent_at", "Alert sent"],
  ["investigation_done_at", "Investigation done"],
  ["action_chosen_at", "Action chosen"],
  ["action_started_at", "Action started"],
  ["action_verified_at", "Action verified"],
  ["report_sent_at", "Report sent"],
  ["closed_at", "Closed"],
];

const STATUS_BADGE: Record<string, string> = {
  open: "bg-red-100 text-red-800",
  contained: "bg-green-100 text-green-800",
  closed: "bg-gray-100 text-gray-700",
};

const ordinal = (n: number) => (n === 2 ? "2nd" : n === 3 ? "3rd" : `${n}th`);

const shortArn = (arn?: string | null) => (arn ? arn.split("/").slice(-1)[0] || arn : "");

export type TokenInfo = { name: string; classLabel: string };

// "Nth attempt" is only meaningful when this incident is linked to an earlier one.
const ATTEMPT_BADGE = "rounded border border-gray-300 bg-white px-2 py-0.5 text-xs font-medium text-gray-600";

/** Last characters of an id/ARN; the full value is in the tooltip and click copies it. */
export function Short({ value, n = 12, className = "" }: { value: string; n?: number; className?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <span
      title={copied ? "Copied" : `${value} (click to copy)`}
      className={`cursor-copy font-mono ${className}`}
      // Deliberately lets the click bubble: inside a clickable row it copies and still opens the row.
      onClick={() => {
        try {
          void navigator.clipboard?.writeText(value);
        } catch {}
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
    >
      {copied ? <span className="text-green-700">copied</span> : tail(value, n)}
    </span>
  );
}

function When({ iso }: { iso?: string | null }) {
  if (!iso) return <>—</>;
  return <span title={exactUtc(iso)}>{relativeTime(iso)}</span>;
}

// The five moments the talk is about, in order.
const MINI_STEPS: Array<[string, string]> = [
  ["first_event_time", "Event"],
  ["first_alert_sent_at", "Alert"],
  ["investigation_done_at", "Investigated"],
  ["action_started_at", "Action started"],
  ["action_verified_at", "Verified"],
];

const secs = (from?: unknown, to?: unknown): string => {
  const ms = Date.parse(String(to)) - Date.parse(String(from));
  if (!Number.isFinite(ms)) return "";
  const s = ms / 1000;
  if (s === 0) return "0 s";
  if (s < 10) return `+${s.toFixed(1)} s`;
  if (s < 120) return `+${Math.round(s)} s`;
  return `+${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
};

function LatestIncident({ inc, tokenInfo, onOpen }: { inc: Incident; tokenInfo?: (id: string) => TokenInfo | null; onOpen: () => void }) {
  const t = (inc.honeytoken_ids || []).map((id) => ({ id, info: tokenInfo?.(id) ?? null }));
  const origin = inc.first_event_time as string | undefined;
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => { if (e.key === "Enter") onOpen(); }}
      className="mt-4 cursor-pointer rounded-lg border border-blue-200 bg-blue-50/50 p-4 hover:bg-blue-50"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">Latest incident</span>
        <span className={`rounded px-2 py-0.5 text-xs font-medium ${STATUS_BADGE[inc.status] || STATUS_BADGE.closed}`}>{inc.status}</span>
        {inc.related_incident_id && inc.repeat_count > 1 && <span className={ATTEMPT_BADGE}>{ordinal(inc.repeat_count)} attempt</span>}
        <span className="ml-auto text-xs text-gray-500"><When iso={origin} /> · open details →</span>
      </div>
      <div className="mt-2 grid gap-x-8 gap-y-1 text-sm text-gray-800 sm:grid-cols-3">
        <div>
          <div className="text-xs text-gray-500">Honeytoken</div>
          {t.length === 0 ? "—" : t.map(({ id, info }) => (
            <div key={id} className="font-medium">
              {info ? info.name : id.slice(0, 8)}
              {info && <span className="ml-1.5 text-xs font-normal text-gray-500">{info.classLabel}</span>}
            </div>
          ))}
        </div>
        <div>
          <div className="text-xs text-gray-500">Workload</div>
          <div>
            <span className="font-medium">{shortArn(inc.service_arn) || "—"}</span>
            {inc.source_task_id && <span className="text-gray-500"> / <Short value={String(inc.source_task_id)} /></span>}
          </div>
        </div>
        <div>
          <div className="text-xs text-gray-500">Source IP</div>
          <div className="font-mono">{inc.source_ip || "—"}</div>
        </div>
      </div>
      <ol className="mt-4 grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-5 sm:gap-1">
        {MINI_STEPS.map(([field, label], idx) => {
          const v = inc[field] as string | undefined;
          return (
            <li key={field} className="min-w-0">
              <div className="flex items-center">
                <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${v ? "bg-blue-600" : "bg-gray-300"}`} />
                {idx < MINI_STEPS.length - 1 && <span className="ml-1 h-px flex-1 bg-gray-300" />}
              </div>
              <div className="mt-1 whitespace-nowrap text-[11px] leading-tight text-gray-500 sm:text-xs">{label}</div>
              <div className="font-mono text-xs font-semibold text-gray-900 sm:text-sm" title={v ? exactUtc(v) : undefined}>
                {v ? (field === "first_event_time" ? "0 s" : secs(origin, v)) : "—"}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function delta(from: string | undefined, to: string): string {
  if (!from) return "";
  const ms = Date.parse(to) - Date.parse(from);
  if (!Number.isFinite(ms)) return "";
  if (ms === 0) return "+0s";
  const sign = ms < 0 ? "-" : "+";
  const s = Math.abs(ms) / 1000;
  if (s < 60) return `${sign}${s.toFixed(s < 10 ? 1 : 0)}s`;
  if (s < 3600) return `${sign}${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  return `${sign}${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
}


export default function HoneypotIncidents({
  show = "table",
  tokenInfo,
  onLoaded,
}: {
  show?: "latest" | "table";
  tokenInfo?: (id: string) => TokenInfo | null;
  onLoaded?: (incidents: Incident[]) => void;
}) {
  const readOnly = useReadOnly();
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [selected, setSelected] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [summarizing, setSummarizing] = useState(false);
  const [report, setReport] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const resp = await fetch("/api/honeypots/incidents?limit=50");
      const body = await resp.json().catch(() => []);
      const list: Incident[] = Array.isArray(body) ? body : [];
      setIncidents(list);
      onLoaded?.(list);
    } catch (e) {
      setError(String(e));
    }
  }, [onLoaded]);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  const open = async (id: string) => {
    setError(null);
    setReport(null);
    const resp = await fetch(`/api/honeypots/incidents/${id}`);
    if (!resp.ok) {
      setError(`Could not load incident (${resp.status})`);
      return;
    }
    setSelected(await resp.json());
  };

  const close = async (id: string) => {
    const resp = await fetch(`/api/honeypots/incidents/${id}/close`, { method: "POST" });
    if (!resp.ok) {
      setError(`Close failed (${resp.status})`);
      return;
    }
    await load();
    await open(id);
  };

  const summarize = async (id: string) => {
    setSummarizing(true);
    setError(null);
    try {
      const resp = await fetch(`/api/honeypots/incidents/${id}/summarize`, { method: "POST" });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        setError(body.detail || `Summary failed (${resp.status})`);
        return;
      }
      await open(id);
    } finally {
      setSummarizing(false);
    }
  };

  const showReport = async (id: string) => {
    const resp = await fetch(`/api/honeypots/incidents/${id}/report`);
    if (!resp.ok) {
      setError(`Could not load report (${resp.status})`);
      return;
    }
    setReport((await resp.json()).markdown || "");
  };

  const latest = incidents.reduce<Incident | null>(
    (best, i) => (!best || Date.parse(String(i.first_event_time)) > Date.parse(String(best.first_event_time)) ? i : best),
    null,
  );
  const inc = selected?.incident;
  const timeline = inc ? TIMELINE.filter(([f]) => typeof inc[f] === "string" && inc[f]) : [];
  const origin = inc?.first_event_time as string | undefined;

  return (
    <section>
      {show === "table" && (
        <>
          <h2 className="text-lg font-semibold text-gray-900">Incidents</h2>
          <p className="text-sm text-gray-600">One incident per intrusion: repeated trips from the same task are grouped, not re-announced.</p>
        </>
      )}

      {error && <div className="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}

      {show === "latest" && (
        latest
          ? <LatestIncident inc={latest} tokenInfo={tokenInfo} onOpen={() => open(latest.incident_id)} />
          : <p className="mt-4 text-sm text-gray-500">No incidents yet. The first trip of a honeytoken shows up here.</p>
      )}

      {show === "table" && (
      <div className="mt-4 overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="rt m-0 min-w-full text-sm shadow-none">
          <thead className="bg-gray-50 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
            <tr>
              <th className="px-3 py-2">First event</th>
              <th className="px-3 py-2">Honeytokens</th>
              <th className="px-3 py-2">Service / task</th>
              <th className="px-3 py-2">Alerts</th>
              <th className="px-3 py-2">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {incidents.length === 0 && (
              <tr>
                <td colSpan={5} className="rt-block px-3 py-4 text-center text-gray-500">No incidents.</td>
              </tr>
            )}
            {incidents.map((i) => (
              <tr key={i.incident_id} onClick={() => open(i.incident_id)} className="cursor-pointer hover:bg-gray-50">
                <td data-label="First event" className="px-3 py-2 whitespace-nowrap"><When iso={i.first_event_time as string} /></td>
                <td data-label="Honeytokens" className="px-3 py-2">
                  <div className="min-w-0">
                  {(i.honeytoken_ids || []).map((id) => {
                    const info = tokenInfo?.(id);
                    return info ? (
                      <div key={id}>
                        {info.name} <span className="text-xs text-gray-500">{info.classLabel}</span>
                      </div>
                    ) : (
                      <div key={id} className="font-mono text-xs" title={id}>{id.slice(0, 8)}</div>
                    );
                  })}
                  </div>
                </td>
                <td data-label="Service / task" className="px-3 py-2">
                  <span className="min-w-0 break-all">
                  {shortArn(i.service_arn) || "—"}
                  {i.source_task_id ? <span className="text-gray-500"> / <Short value={String(i.source_task_id)} /></span> : i.source_ip ? <span className="text-gray-500"> / {i.source_ip}</span> : null}
                  </span>
                </td>
                <td data-label="Alerts" className="px-3 py-2">{i.alert_count}</td>
                <td data-label="Status" className="px-3 py-2 whitespace-nowrap">
                  <span className={`rounded px-2 py-0.5 text-xs font-medium ${STATUS_BADGE[i.status] || STATUS_BADGE.closed}`}>{i.status}</span>
                  {i.related_incident_id && i.repeat_count > 1 && (
                    <span className={`ml-2 ${ATTEMPT_BADGE}`}>{ordinal(i.repeat_count)} attempt</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      )}

      {selected && inc && (
        <div className="fixed inset-0 z-50 flex justify-end bg-black/30" onClick={() => setSelected(null)}>
          <aside className="h-full w-full overflow-y-auto bg-white p-4 shadow-xl sm:w-[560px] sm:p-5" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between">
              <div>
                <h3 className="text-base font-semibold text-gray-900">Incident</h3>
                <div className="break-all font-mono text-xs text-gray-500">{inc.incident_id}</div>
              </div>
              <button onClick={() => setSelected(null)} aria-label="Close" className="bg-transparent px-2 py-0 text-xl leading-none text-gray-400 hover:bg-transparent hover:text-gray-600">&times;</button>
            </div>

            <div className="mt-2 flex flex-wrap items-center gap-2">
              <span className={`rounded px-2 py-0.5 text-xs font-medium ${STATUS_BADGE[inc.status] || STATUS_BADGE.closed}`}>{inc.status}</span>
              {inc.related_incident_id && inc.repeat_count > 1 && (
                <span className={ATTEMPT_BADGE}>
                  {ordinal(inc.repeat_count)} attempt{" "}
                  (<button className={LINK_BTN} onClick={() => open(inc.related_incident_id as string)}>previous</button>)
                </span>
              )}
              {inc.status !== "closed" && (
                <button onClick={() => close(inc.incident_id)} {...writeProps(readOnly)} className="ml-auto rounded border border-gray-300 bg-white px-3 py-1 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50">
                  Close incident
                </button>
              )}
            </div>

            {inc.summary && inc.summary.paragraph ? (
              <div className="mt-3 text-sm text-gray-800">
                <p>{inc.summary.paragraph}</p>
                <ul className="mt-2 list-disc space-y-1 pl-5">
                  {(inc.summary.bullets || []).map((b, n) => (
                    <li key={n}>{b}</li>
                  ))}
                </ul>
              </div>
            ) : (
              <button
                {...writeProps(readOnly, summarizing)}
                onClick={() => summarize(inc.incident_id)}
                className="mt-3 rounded border border-gray-300 bg-white px-3 py-1 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
              >
                {summarizing ? "Generating..." : "Generate summary"}
              </button>
            )}
            <button onClick={() => (report ? setReport(null) : showReport(inc.incident_id))} className="mt-3 block bg-transparent p-0 text-sm text-blue-700 underline hover:bg-transparent">
              {report ? "Hide report" : "Report"}
            </button>
            {report && <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap rounded border border-gray-200 bg-gray-50 p-3 text-xs text-gray-800">{report}</pre>}

            <h4 className="mt-5 text-sm font-semibold text-gray-900">Timeline</h4>
            <ol className="mt-2 space-y-1 text-sm">
              {timeline.map(([field, label]) => (
                <li key={field} className="flex justify-between gap-3">
                  <span className="shrink-0 text-gray-700">{label}</span>
                  <span className="text-right text-gray-500">
                    {exactUtc(inc[field] as string)}
                    {field !== "first_event_time" && <span className="ml-2 font-mono text-xs">{delta(origin, inc[field] as string)}</span>}
                  </span>
                </li>
              ))}
            </ol>

            <h4 className="mt-5 text-sm font-semibold text-gray-900">Alerts ({selected.alerts.length})</h4>
            <ul className="mt-2 max-h-56 space-y-1 overflow-y-auto text-sm">
              {selected.alerts.map((a) => (
                <li key={a.alert_id} className="text-gray-700">
                  <span className="text-gray-500"><When iso={a.triggered_at} /></span> {tokenInfo?.(a.honeytoken_id)?.name ?? a.honeytoken_id.slice(0, 8)}
                  {(a.event_name || a.service) && <span className="text-gray-500"> · {a.event_name || a.service}</span>}
                  {a.source_ip && <span className="text-gray-500"> · {a.source_ip}</span>}
                </li>
              ))}
            </ul>

            {(() => {
              const action = (inc.action || null) as null | {
                plan?: { action_ids?: string[]; rationale?: string; chosen_by?: string };
                decisions?: { action_id: string; action_type: string; decision: string; reason?: string; risk?: number; risk_max?: number; mode?: string; status?: string }[];
                pending?: { action_id: string; action_type: string; risk?: number; reason?: string }[];
              };
              if (!action) return null;
              const approve = async (actionId: string) => {
                const resp = await fetch(`/api/honeypots/incidents/${inc.incident_id}/approve`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ action_id: actionId }),
                });
                if (!resp.ok) {
                  const body = await resp.json().catch(() => ({}));
                  setError(body.detail || `Approve failed (${resp.status})`);
                }
                await open(inc.incident_id);
              };
              return (
                <>
                  <h4 className="mt-5 text-sm font-semibold text-gray-900">Decision</h4>
                  {action.plan?.rationale && (
                    <p className="mt-1 text-xs text-gray-500">
                      Chosen by {action.plan.chosen_by || "policy"}: {action.plan.rationale}
                    </p>
                  )}
                  <ul className="mt-2 space-y-1 text-sm">
                    {(action.decisions || []).map((d) => (
                      <li key={d.action_id} className="text-gray-700">
                        <span className="font-medium">{d.action_type}</span> · risk {d.risk}/{d.risk_max} · {d.mode ?? ""} → {d.status || d.decision}
                        {d.reason && <span className="text-gray-500"> · {d.reason}</span>}
                      </li>
                    ))}
                  </ul>
                  {(action.pending || []).length > 0 && (
                    <ul className="mt-2 space-y-1 text-sm">
                      {(action.pending || []).map((p) => (
                        <li key={p.action_id} className="flex items-center gap-2 text-gray-700">
                          <span className="font-medium">{p.action_type}</span> · risk {p.risk} · waiting for approval
                          <button onClick={() => approve(p.action_id)} {...writeProps(readOnly)} className="rounded bg-gray-900 px-2 py-0.5 text-xs text-white hover:bg-gray-800 disabled:opacity-50">
                            Approve and run
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              );
            })()}

            <h4 className="mt-5 text-sm font-semibold text-gray-900">Response actions</h4>
            {selected.audits.length === 0 ? (
              <p className="mt-2 text-sm text-gray-500">None yet.</p>
            ) : (
              <ul className="mt-2 space-y-1 text-sm">
                {selected.audits.map((a) => (
                  <li key={a.audit_id} className="text-gray-700">
                    <span className="font-medium">{a.action_type}</span> · {a.status}
                    {a.verify && <span className={a.verify.ok ? " text-green-700" : " text-amber-700"}> · verify {a.verify.ok ? "ok" : "failed"}{a.verify.new_task ? ` (${a.verify.new_task})` : ""}</span>}
                    {a.error && <span className="text-red-700"> · {a.error}</span>}
                  </li>
                ))}
              </ul>
            )}
          </aside>
        </div>
      )}
    </section>
  );
}
