"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { BTN_OUTLINE, BTN_OUTLINE_SM, BTN_QUIET_DANGER_SM, LINK_BTN, exactUtc, relativeTime, useReadOnly, writeProps } from "./honeypotFormat";
import { TYPE_LABEL, prPath, type Decision, type PRRecord, type PendingRevision, type Revision } from "./prTypes";

/**
 * One pull request: why it exists (the intent), what Compass knows (origin
 * context), the timeline of decisions and revisions with the guard checklist,
 * and the actions a person can take (address, approve, pause).
 */

type Context = {
  summary?: string | null;
  entities: Array<Record<string, unknown>>;
  evidence: Array<{ source: string; value: unknown }>;
  history: Array<{ at?: number | string; phase?: string; message?: string }>;
  changed_since_pr: string[];
  notes: string[];
};

type TimelineEntry = { at: number; kind: "revision" | "decision"; revision?: Revision; decision?: Decision };

const OUTCOME_BADGE: Record<string, string> = {
  pushed: "bg-green-100 text-green-800",
  refused: "bg-amber-100 text-amber-800",
  failed: "bg-red-100 text-red-800",
};

const DECISION_TEXT: Record<string, string> = {
  run: "Policy allowed the revision",
  wait_for_approval: "Revision planned, waiting for approval",
  recommend_only: "Mode is off: recorded, nothing pushed",
  blocked: "Revision blocked",
  nothing_to_do: "No actionable comments",
  dismissed: "Pending revision dismissed",
};

const short = (s?: string | null) => (s ? s.slice(0, 7) : "");

function Section({ title, children, right }: { title: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <section className="rounded-lg border bg-white p-4 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
        {right}
      </div>
      {children}
    </section>
  );
}

function GuardChecklist({ revision }: { revision: Revision }) {
  const checks = revision.guard?.checks ?? [];
  if (!checks.length) return null;
  return (
    <ul className="mt-1 grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-0.5 text-xs">
      {checks.map((c) => (
        <li key={c.name} className={c.ok ? "text-gray-600" : "text-amber-700"}>
          {c.ok ? "✓" : "✗"} {c.name.replace(/_/g, " ")}
          {!c.ok && c.detail && <span className="text-gray-500"> — {c.detail}</span>}
        </li>
      ))}
    </ul>
  );
}

function RevisionEntry({ revision, url }: { revision: Revision; url?: string | null }) {
  const [open, setOpen] = useState(!revision.guard?.ok);
  const n = revision.addressed?.length ?? 0;
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2 flex-wrap">
        <span className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${OUTCOME_BADGE[revision.outcome] ?? "bg-gray-100 text-gray-700"}`}>
          Revision {revision.outcome}
        </span>
        {revision.reason && <span className="text-xs text-amber-700">{String(revision.reason).replace(/_/g, " ")}</span>}
        {revision.commit_sha && (
          <a href={url && revision.commit_sha ? `${url.replace(/\/pull\/\d+.*/, "")}/commit/${revision.commit_sha}` : "#"} target="_blank" rel="noopener noreferrer" className="text-xs text-blue-600 hover:underline font-mono">
            {short(revision.commit_sha)}
          </a>
        )}
        <span className="text-xs text-gray-500">
          {revision.trigger} · {n ? `addressed ${n} comment${n === 1 ? "" : "s"}` : "no comments"}{typeof revision.risk === "number" ? ` · risk ${revision.risk}` : ""}
        </span>
        {revision.guard?.checks?.length ? (
          <button type="button" className={`${LINK_BTN} text-xs`} onClick={() => setOpen((o) => !o)}>
            {open ? "hide guard" : "show guard"}
          </button>
        ) : null}
      </div>
      {open && <GuardChecklist revision={revision} />}
      {revision.blockers?.length ? (
        <ul className="text-xs text-amber-700 list-disc ml-4">{revision.blockers.map((b, i) => <li key={i}>{b}</li>)}</ul>
      ) : null}
      {revision.files?.length ? <div className="text-xs text-gray-500 font-mono truncate">{revision.files.join(", ")}</div> : null}
      {revision.note && <div className="text-xs text-gray-600">{revision.note}</div>}
    </div>
  );
}

export default function PRDrawer({ repo, number, onClose, onChanged }: { repo: string; number: number; onClose: () => void; onChanged: () => void }) {
  const readOnly = useReadOnly();
  const [pr, setPr] = useState<PRRecord | null>(null);
  const [context, setContext] = useState<Context | null>(null);
  const [contextOpen, setContextOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [addressOpen, setAddressOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(prPath(repo, number));
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setPr(await res.json());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load");
    }
  }, [repo, number]);

  useEffect(() => {
    void load();
    const id = setInterval(() => void load(), 10_000);
    return () => clearInterval(id);
  }, [load]);

  useEffect(() => {
    if (!contextOpen || context) return;
    fetch(`${prPath(repo, number)}/context`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setContext)
      .catch((e) => setContext({ summary: null, entities: [], evidence: [], history: [], changed_since_pr: [], notes: [String(e)] }));
  }, [contextOpen, context, repo, number]);

  const post = async (suffix: string, body?: unknown, label?: string) => {
    setBusy(suffix);
    setNotice(null);
    try {
      const res = await fetch(`${prPath(repo, number)}${suffix}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
      setNotice(label ?? "Done");
      await load();
      onChanged();
      return data;
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(null);
    }
  };

  const timeline = useMemo<TimelineEntry[]>(() => {
    const entries: TimelineEntry[] = [];
    for (const r of pr?.revisions ?? []) entries.push({ at: r.at, kind: "revision", revision: r });
    for (const d of pr?.decisions ?? []) {
      if (d.decision === "run") continue; // the revision entry says it
      entries.push({ at: d.at, kind: "decision", decision: d });
    }
    return entries.sort((a, b) => b.at - a.at);
  }, [pr]);

  const pending = pr && typeof pr.pending_revision === "object" ? (pr.pending_revision as PendingRevision | null) : null;
  const watch = pr?.watch?.status ?? "watching";
  const isOpen = (pr?.state ?? "open") === "open";
  const wp = (disabled = false) => writeProps(readOnly, disabled || busy !== null);

  return (
    <div className="fixed inset-0 z-40" role="dialog" aria-modal="true">
      <div className="absolute inset-0 bg-black/30" onClick={onClose} />
      <aside className="absolute right-0 top-0 h-full w-full max-w-2xl overflow-y-auto bg-gray-50 shadow-xl p-4 sm:p-6 space-y-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-xs font-medium text-gray-500">{pr ? TYPE_LABEL[pr.type] ?? pr.type : ""}</div>
            <h2 className="text-lg font-semibold text-gray-900 truncate">{pr?.title || `${repo}#${number}`}</h2>
            <div className="flex items-center gap-2 flex-wrap text-xs text-gray-600">
              {pr?.url && (
                <a href={pr.url} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline font-medium">{repo}#{number}</a>
              )}
              {pr?.branch && <span className="font-mono">{pr.branch} → {pr.base_branch}</span>}
              {pr?.state && <span className="uppercase tracking-wide">{pr.state}</span>}
            </div>
          </div>
          <button type="button" className={BTN_OUTLINE_SM} onClick={onClose}>Close</button>
        </div>

        {error && <div className="rounded border border-red-200 bg-red-50 text-red-700 px-3 py-2 text-sm">{error}</div>}
        {notice && <div className="rounded border border-gray-200 bg-white px-3 py-2 text-sm text-gray-700">{notice}</div>}

        {pr && (
          <Section
            title="Watcher"
            right={
              <div className="flex gap-2">
                {isOpen && (
                  <button type="button" className={BTN_OUTLINE_SM} {...wp()} onClick={() => void post("/sync", {}, "Sync queued")}>Sync now</button>
                )}
                {isOpen && watch === "watching" && (
                  <button type="button" className={BTN_OUTLINE_SM} {...wp()} onClick={() => void post("/pause", {}, "Paused")}>Pause</button>
                )}
                {isOpen && (watch === "paused" || watch === "needs_human") && (
                  <button type="button" className={BTN_OUTLINE_SM} {...wp()} onClick={() => void post("/resume", {}, "Watching again")}>Resume</button>
                )}
                {isOpen && watch !== "paused" && (
                  <button type="button" className={BTN_OUTLINE_SM} {...wp()} onClick={() => setAddressOpen((o) => !o)}>Address…</button>
                )}
              </div>
            }
          >
            <div className="text-sm text-gray-700">
              {watch === "needs_human" ? (
                <>Waiting for a person: <span className="font-medium">{String(pr.watch?.reason ?? "").replace(/_/g, " ")}</span>. Resume to let Compass push again.</>
              ) : watch === "paused" ? "Paused: no replies, no pushes." : watch === "done" ? "Done watching." : "Watching for reviews, checks and merges."}
              <span className="text-gray-500"> · {pr.counters?.revisions ?? 0} revision{(pr.counters?.revisions ?? 0) === 1 ? "" : "s"}, {pr.counters?.refusals ?? 0} refused</span>
            </div>
            {addressOpen && (
              <div className="mt-2 space-y-2">
                <textarea
                  className="w-full rounded border border-gray-300 p-2 text-sm"
                  rows={3}
                  placeholder="What should change? Leave empty to address the open review comments from collaborators."
                  value={message}
                  onChange={(e) => setMessage(e.target.value)}
                />
                <div className="flex gap-2">
                  <button
                    type="button"
                    className={BTN_OUTLINE}
                    {...wp()}
                    onClick={() => void post("/address", { message: message.trim() || undefined, fetch_comments: !message.trim(), confirm: true }, "Revision queued").then(() => { setMessage(""); setAddressOpen(false); })}
                  >
                    {message.trim() ? "Revise with this request" : "Address open comments"}
                  </button>
                  <button type="button" className={BTN_OUTLINE_SM} onClick={() => setAddressOpen(false)}>Cancel</button>
                </div>
              </div>
            )}
          </Section>
        )}

        {pending && (
          <Section title="Revision waiting for approval">
            <div className="text-sm text-gray-700">
              Compass proposes to address {pending.comments.length} request{pending.comments.length === 1 ? "" : "s"} in <span className="font-mono text-xs">{pending.files.join(", ") || "the PR's files"}</span> · risk {pending.risk.risk} (max {pending.risk_max}, mode {pending.mode}).
            </div>
            <ul className="text-xs text-gray-600 space-y-1">
              {pending.comments.map((c) => (
                <li key={String(c.id)}>
                  <span className="font-medium">{c.author ?? "reviewer"}</span>{c.path ? <span className="font-mono"> {c.path}{c.line ? `:${c.line}` : ""}</span> : null}: {c.body?.slice(0, 200)}
                </li>
              ))}
            </ul>
            <div className="flex gap-2">
              <button type="button" className={BTN_OUTLINE} {...wp()} onClick={() => void post("/revision/approve", {}, "Approved, revision queued")}>Approve</button>
              <button type="button" className={BTN_QUIET_DANGER_SM} {...wp()} onClick={() => void post("/revision/dismiss", {}, "Dismissed")}>Dismiss</button>
            </div>
          </Section>
        )}

        {pr?.intent && (
          <Section title="Why this pull request exists">
            <p className="text-sm text-gray-800">{pr.intent.goal}</p>
            {pr.intent.done_criteria?.length ? (
              <ul className="text-xs text-gray-600 list-disc ml-4">{pr.intent.done_criteria.map((d, i) => <li key={i}>{d}</li>)}</ul>
            ) : null}
            {pr.intent.invariants_text?.length ? (
              <ul className="text-xs text-gray-700 space-y-0.5">{pr.intent.invariants_text.map((t, i) => <li key={i}>🔒 {t}</li>)}</ul>
            ) : null}
            {pr.intent.scope?.files?.length ? (
              <div className="text-xs text-gray-500">Only touches: <span className="font-mono">{pr.intent.scope.files.join(", ")}</span></div>
            ) : null}
            <div className="text-xs text-gray-500">
              {typeof pr.intent.risk === "number" ? `Risk at creation ${pr.intent.risk} · ` : ""}
              origin {pr.origin?.captain}{pr.origin?.finding_id ? ` · finding ${pr.origin.finding_id}` : ""}{pr.origin?.honeytoken_id ? ` · honeytoken ${pr.origin.honeytoken_id}` : ""}{pr.origin?.rope_id ? ` · request ${pr.origin.rope_id}` : ""}
            </div>
          </Section>
        )}

        {pr && (
          <Section
            title="What Compass knows"
            right={<button type="button" className={`${LINK_BTN} text-xs`} onClick={() => setContextOpen((o) => !o)}>{contextOpen ? "hide" : "load"}</button>}
          >
            {!contextOpen ? (
              <p className="text-xs text-gray-500">The finding, triage, request, honeytoken or resources behind this change, read fresh from Compass's own records.</p>
            ) : !context ? (
              <p className="text-xs text-gray-500">Loading…</p>
            ) : (
              <div className="space-y-2">
                {context.summary && <p className="text-sm text-gray-800">{context.summary}</p>}
                {context.changed_since_pr.length ? (
                  <ul className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded p-2 space-y-0.5">
                    {context.changed_since_pr.map((c, i) => <li key={i}>Changed since the PR opened: {c}</li>)}
                  </ul>
                ) : null}
                {context.entities.map((e, i) => (
                  <details key={i} className="text-xs text-gray-700">
                    <summary className="cursor-pointer">{String(e.kind ?? "entity")} {String(e.title ?? e.name ?? e.id ?? "")}</summary>
                    <pre className="mt-1 whitespace-pre-wrap break-words rounded bg-gray-100 p-2 text-[11px]">{JSON.stringify(e, null, 2)}</pre>
                  </details>
                ))}
                {context.evidence.map((e, i) => (
                  <details key={`ev-${i}`} className="text-xs text-gray-700">
                    <summary className="cursor-pointer">evidence: {e.source}</summary>
                    <pre className="mt-1 whitespace-pre-wrap break-words rounded bg-gray-100 p-2 text-[11px]">{JSON.stringify(e.value, null, 2)}</pre>
                  </details>
                ))}
                {context.history.length ? (
                  <ul className="text-[11px] text-gray-500 space-y-0.5">{context.history.slice(-8).map((h, i) => <li key={i}>{h.phase ? `${h.phase}: ` : ""}{h.message}</li>)}</ul>
                ) : null}
                {context.notes.length ? <div className="text-[11px] text-gray-400">{context.notes.join(" · ")}</div> : null}
              </div>
            )}
          </Section>
        )}

        {pr && (
          <Section title="Timeline">
            {timeline.length === 0 ? (
              <p className="text-xs text-gray-500">Opened {relativeTime(pr.created_at ?? null)}. Nothing has happened since.</p>
            ) : (
              <ol className="space-y-3">
                {timeline.map((entry, i) => (
                  <li key={i} className="flex gap-3">
                    <div className="w-20 shrink-0 text-[11px] text-gray-400" title={exactUtc(entry.at)}>{relativeTime(entry.at)}</div>
                    <div className="min-w-0 flex-1">
                      {entry.kind === "revision" && entry.revision ? (
                        <RevisionEntry revision={entry.revision} url={pr.url} />
                      ) : entry.decision ? (
                        <div className="text-xs text-gray-600">
                          {DECISION_TEXT[entry.decision.decision] ?? entry.decision.decision}
                          {entry.decision.reason ? ` — ${String(entry.decision.reason).replace(/_/g, " ")}` : ""}
                          {typeof entry.decision.risk === "number" ? ` · risk ${entry.decision.risk}` : ""}
                          {entry.decision.trigger ? ` · ${entry.decision.trigger}` : ""}
                        </div>
                      ) : null}
                    </div>
                  </li>
                ))}
                <li className="flex gap-3">
                  <div className="w-20 shrink-0 text-[11px] text-gray-400" title={exactUtc(pr.created_at ?? null)}>{relativeTime(pr.created_at ?? null)}</div>
                  <div className="text-xs text-gray-600">Pull request opened by Compass</div>
                </li>
              </ol>
            )}
          </Section>
        )}
      </aside>
    </div>
  );
}
