"use client";
// The feed: every item that needs a person, newest first, with its primary action as
// a button, the rest in a small menu, and a chevron that expands the detail. One
// component renders a tab's "Needs you" view (`tab`) and the whole feed. It knows no
// kinds: actions come from the item, and a detail renderer exists per kind only to lay
// the data out; a kind without one renders as key–value rows. docs/inbox.md.
import Link from "next/link";
import React, { useEffect, useMemo, useState } from "react";

import { BTN_OUTLINE_SM, relativeTime } from "./honeypotFormat";
import { itemsForTab, outcomeText, primaryAction, runInboxAction, useInbox } from "../lib/inbox";
import type { InboxAction, InboxItem } from "../lib/inbox";

const VERB_CHIP: Record<string, string> = {
  approve: "bg-blue-50 text-blue-700",
  fix: "bg-amber-50 text-amber-800",
  merge: "bg-purple-50 text-purple-700",
  later: "bg-gray-100 text-gray-600",
};
const STRIPE: Record<string, string> = { urgent: "border-l-red-500", normal: "border-l-amber-400", info: "border-l-transparent" };
const BTN_PRIMARY_SM = "inline-flex items-center justify-center rounded-md px-2.5 py-1 text-xs font-semibold bg-gray-900 text-white hover:opacity-90 transition disabled:opacity-50 disabled:cursor-not-allowed";
const OUTCOME_MS = 5000;

type Outcome = { id: string; text: string; ok: boolean; title: string; until: number };

const isPlain = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const present = (v: unknown) => v !== null && v !== undefined && v !== "" && !(Array.isArray(v) && v.length === 0) && !(isPlain(v) && Object.keys(v).length === 0);
const words = (key: string) => key.replace(/[_.]+/g, " ");

function Code({ value }: { value: unknown }) {
  return <pre className="max-h-64 overflow-auto rounded bg-gray-50 p-2 text-[11px] leading-snug text-gray-800">{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre>;
}

function Rows({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data).filter(([, v]) => present(v));
  if (entries.length === 0) return <p className="text-xs text-gray-500">Nothing more to show.</p>;
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-xs">
      {entries.map(([k, v]) => (
        <React.Fragment key={k}>
          <dt className="text-gray-500">{words(k)}</dt>
          <dd className="min-w-0 break-words text-gray-800">{typeof v === "string" || typeof v === "number" || typeof v === "boolean" ? String(v) : <Code value={v} />}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}

function RiskDetail({ detail }: { detail: Record<string, unknown> }) {
  const risk = isPlain(detail.risk) ? detail.risk : {};
  const breakdown = isPlain(risk.breakdown) ? risk.breakdown : {};
  const notes = Array.isArray(risk.notes) ? risk.notes : [];
  const incident = isPlain(detail.incident) ? detail.incident : {};
  return (
    <div className="space-y-2 text-xs">
      <div>
        <span className="font-semibold text-gray-800">Risk {String(risk.score ?? "—")}</span>
        {Object.keys(breakdown).length > 0 && (
          <ul className="mt-1 grid grid-cols-2 gap-x-4 sm:grid-cols-3">
            {Object.entries(breakdown).map(([k, v]) => (
              <li key={k} className="flex justify-between gap-2 text-gray-600"><span>{words(k)}</span><span className="font-mono">{typeof v === "number" ? v.toFixed(2) : String(v)}</span></li>
            ))}
          </ul>
        )}
        {notes.length > 0 && <p className="mt-1 text-gray-500">{notes.map(String).join(" · ")}</p>}
      </div>
      {present(detail.reason) && <p className="text-gray-700">Parked: {String(detail.reason)}</p>}
      <Rows data={{ ...(isPlain(detail.context) ? detail.context : {}), ...incident }} />
    </div>
  );
}

function CommentsDetail({ detail }: { detail: Record<string, unknown> }) {
  const comments = Array.isArray(detail.comments) ? detail.comments : [];
  return (
    <div className="space-y-2 text-xs">
      {comments.map((c: any, i: number) => (
        <blockquote key={i} className="border-l-2 border-gray-300 pl-3 text-gray-700">
          {c?.author && <span className="font-semibold">{String(c.author)}: </span>}
          {String(c?.body ?? c?.text ?? JSON.stringify(c))}
        </blockquote>
      ))}
      <Rows data={{ plan: detail.plan, files: detail.files, risk: detail.risk }} />
    </div>
  );
}

function LogDetail({ detail }: { detail: Record<string, unknown> }) {
  const events = Array.isArray(detail.events) ? detail.events : [];
  return (
    <div className="space-y-2 text-xs">
      {present(detail.error) && <p className="break-words font-mono text-red-700">{String(detail.error)}</p>}
      {events.length > 0 && (
        <ol className="space-y-0.5 font-mono text-[11px] text-gray-600">
          {events.map((e: any, i: number) => (
            <li key={i}><span className="text-gray-400">{e?.timestamp ? relativeTime(e.timestamp) : ""}</span> {String(e?.message ?? JSON.stringify(e))}</li>
          ))}
        </ol>
      )}
      <Rows data={{ command: detail.command, requester: detail.requester, status: detail.status }} />
    </div>
  );
}

function StatementsDetail({ detail }: { detail: Record<string, unknown> }) {
  const { statements, ...rest } = detail;
  return (
    <div className="space-y-2 text-xs">
      {present(statements) && <Code value={statements} />}
      <Rows data={rest} />
    </div>
  );
}

function NotesDetail({ detail }: { detail: Record<string, unknown> }) {
  const notes = Array.isArray(detail.notes) ? detail.notes : [];
  const { notes: _n, ...rest } = detail;
  return (
    <div className="space-y-2 text-xs">
      {notes.length > 0 && <ul className="list-disc pl-4 text-gray-700">{notes.map((n: any, i: number) => <li key={i}>{String(n?.version ?? "")} {String(n?.title ?? "")}</li>)}</ul>}
      <Rows data={rest} />
    </div>
  );
}

function Detail({ item }: { item: InboxItem }) {
  const detail = isPlain(item.detail) ? item.detail : {};
  if (item.kind === "decoy.approval") return <RiskDetail detail={detail} />;
  if (item.kind === "pr.revision") return <CommentsDetail detail={detail} />;
  if (item.kind === "job.failed") return <LogDetail detail={detail} />;
  if (item.kind === "grant.proposed" || item.kind === "grant.waiting") return <StatementsDetail detail={detail} />;
  if (item.kind === "update.available") return <NotesDetail detail={detail} />;
  return <Rows data={detail} />;
}

function ActionButton({ action, busy, className, onRun }: { action: InboxAction; busy: boolean; className: string; onRun: (a: InboxAction) => void }) {
  if (action.href && !action.method) {
    const external = /^https?:/.test(action.href);
    return external ? (
      <a href={action.href} target="_blank" rel="noopener noreferrer" className={className}>{action.label} ↗</a>
    ) : (
      <Link href={action.href} className={className}>{action.label}</Link>
    );
  }
  return <button type="button" disabled={busy} onClick={() => onRun(action)} className={className}>{action.label}</button>;
}

function Row({ item, onOutcome, readOnly }: { item: InboxItem; onOutcome: (o: Outcome) => void; readOnly: boolean }) {
  const [open, setOpen] = useState(false);
  const [menu, setMenu] = useState(false);
  const [confirming, setConfirming] = useState<InboxAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const primary = primaryAction(item);
  const rest = item.actions.filter((a) => a !== primary);

  const run = async (action: InboxAction) => {
    setMenu(false);
    if (action.confirm && confirming?.key !== action.key) {
      setConfirming(action);
      return;
    }
    setConfirming(null);
    setBusy(true);
    setError(null);
    try {
      const result = await runInboxAction(action);
      onOutcome({ id: item.id, title: item.title, text: outcomeText(action, result), ok: true, until: Date.now() + OUTCOME_MS });
    } catch (e: any) {
      setError(e?.message || "The action failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <li data-inbox-item={item.id} data-inbox-kind={item.kind} data-inbox-verb={item.verb} className={`rounded-lg border border-l-4 bg-white ${STRIPE[item.severity] || STRIPE.info}`}>
      <div className="flex items-start gap-3 px-4 py-3">
        <button type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open} aria-label={open ? "Hide detail" : "Show detail"} className="mt-0.5 shrink-0 rounded p-0.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700">
          <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={`transition-transform ${open ? "rotate-90" : ""}`}><path d="M7 5l6 5-6 5V5z" /></svg>
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${VERB_CHIP[item.verb] || VERB_CHIP.later}`}>{item.verb}</span>
            <span className="truncate text-sm font-medium text-gray-900">{item.title}</span>
            <span className="text-xs text-gray-400" title={new Date(item.since).toISOString()}>{item.since ? relativeTime(item.since) : ""}</span>
          </div>
          {item.summary && <p className="mt-0.5 break-words text-xs text-gray-600">{item.summary}</p>}
          {confirming && (
            <div className="mt-2 flex flex-wrap items-center gap-2 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
              <span>{confirming.confirm}</span>
              <button type="button" disabled={busy} onClick={() => void run(confirming)} className={BTN_PRIMARY_SM}>{busy ? "Running…" : `Yes, ${confirming.label.toLowerCase()}`}</button>
              <button type="button" onClick={() => setConfirming(null)} className={BTN_OUTLINE_SM}>Cancel</button>
            </div>
          )}
          {error && <p className="mt-2 text-xs text-red-700">{error}</p>}
          {open && <div className="mt-3 border-t pt-3"><Detail item={item} /></div>}
        </div>
        {!readOnly && (
          <div className="relative flex shrink-0 items-center gap-1">
            {primary && <ActionButton action={primary} busy={busy} className={primary.method ? BTN_PRIMARY_SM : BTN_OUTLINE_SM} onRun={(a) => void run(a)} />}
            {rest.length > 0 && (
              <>
                <button type="button" aria-label="More actions" aria-expanded={menu} onClick={() => setMenu((v) => !v)} className={`${BTN_OUTLINE_SM} px-1.5`}>⋯</button>
                {menu && (
                  <ul className="absolute right-0 top-full z-10 mt-1 min-w-[12rem] rounded-md border bg-white py-1 text-xs shadow-lg" role="menu">
                    {rest.map((a) => (
                      <li key={a.key} role="none">
                        <ActionButton action={a} busy={busy} className="block w-full px-3 py-1.5 text-left text-gray-700 hover:bg-gray-50" onRun={(x) => void run(x)} />
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </li>
  );
}

export default function InboxFeed({ tab, verb, title, empty, readOnly = false }: { tab?: string; verb?: string | null; title?: string; empty?: React.ReactNode; readOnly?: boolean }) {
  const { feed, error, refresh } = useInbox();
  const [outcomes, setOutcomes] = useState<Outcome[]>([]);
  const items = useMemo(() => itemsForTab(feed, tab).filter((i) => !verb || i.verb === verb), [feed, tab, verb]);

  // an outcome row stands in for the item for five seconds, then the feed is what it is
  useEffect(() => {
    if (outcomes.length === 0) return;
    const t = window.setTimeout(() => setOutcomes((rows) => rows.filter((o) => o.until > Date.now())), OUTCOME_MS + 50);
    return () => window.clearTimeout(t);
  }, [outcomes]);
  const onOutcome = (o: Outcome) => {
    setOutcomes((rows) => [o, ...rows.filter((r) => r.id !== o.id)]);
    void refresh();
  };
  const outcomeIds = new Set(outcomes.map((o) => o.id));

  return (
    <section aria-label={title || "Needs you"} data-inbox-feed={tab || "all"} className="space-y-2">
      {title && <h2 className="text-base font-semibold text-gray-900">{title}</h2>}
      {error && <div className="rounded border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}
      {outcomes.map((o) => (
        <div key={`outcome-${o.id}`} role="status" className={`rounded-lg border px-4 py-2 text-xs ${o.ok ? "border-green-200 bg-green-50 text-green-800" : "border-red-200 bg-red-50 text-red-700"}`}>
          <span className="font-medium">{o.title}</span> — {o.text}
        </div>
      ))}
      {feed === null && !error ? (
        <div className="py-6 text-center text-sm text-gray-500">Loading…</div>
      ) : items.filter((i) => !outcomeIds.has(i.id)).length === 0 && outcomes.length === 0 ? (
        <div className="rounded-lg border bg-white px-6 py-10 text-center text-sm text-gray-500">{empty || (tab ? "Nothing needs you here." : "Nothing needs you.")}</div>
      ) : (
        <ul className="space-y-2">
          {items.filter((i) => !outcomeIds.has(i.id)).map((item) => <Row key={item.id} item={item} onOutcome={onOutcome} readOnly={readOnly} />)}
        </ul>
      )}
    </section>
  );
}
