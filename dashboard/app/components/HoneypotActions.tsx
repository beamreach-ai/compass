"use client";

import React, { useCallback, useEffect, useState } from "react";
import { BTN_OUTLINE, BTN_QUIET_DANGER_SM, LINK_BTN, exactUtc, relativeTime, useReadOnly, writeProps } from "./honeypotFormat";
import { Short } from "./HoneypotIncidents";

/**
 * "What Compass may do": the whitelist of response actions for this workspace.
 * The agent only ever chooses from enabled rows, and the responder IAM role in
 * the target account is generated from them, so this list is the whole
 * permission surface. The audit of actions taken lives in HoneypotAuditList
 * (History view).
 */

type ScopeFields = "service_arns" | "user_arns" | "user_path_prefixes";
type Scope = Record<ScopeFields, string[]>;
type Mode = "off" | "approve" | "autopilot";

type CatalogEntry = {
  action_type: string;
  title: string;
  description: string;
  change_type: string;
  rollback_supported: boolean;
  scope_fields: ScopeFields[];
};

type ActionRow = {
  action_id: string;
  action_type: string;
  scope: Scope;
  mode: Mode;
  risk_max: number;
  enabled: boolean;
  catalog: CatalogEntry | null;
};

type Audit = {
  audit_id: string;
  action_type: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  incident_id: string;
  chosen_by: string;
  target?: Record<string, string>;
  verify?: { ok?: boolean; new_task?: string; steady_after_s?: number } | null;
  error?: string | null;
};

const SCOPE_LABELS: Record<ScopeFields, string> = {
  service_arns: "ECS service ARNs",
  user_arns: "IAM user ARNs",
  user_path_prefixes: "IAM user path prefixes (e.g. /service/)",
};

const MODE_HELP: Record<Mode, string> = {
  off: "Recommend only",
  approve: "Wait for one click",
  autopilot: "Run when risk ≤ max",
};

const emptyScope = (): Scope => ({ service_arns: [], user_arns: [], user_path_prefixes: [] });

const STATUS_BADGE: Record<string, string> = {
  verified: "bg-green-100 text-green-800",
  running: "bg-blue-100 text-blue-800",
  verify_failed: "bg-amber-100 text-amber-800",
  failed: "bg-red-100 text-red-800",
  refused: "bg-red-100 text-red-800",
};

/** Read-only preview of the generated responder role module (lives under Settings). */
export function ResponderRolePreview() {
  const [moduleText, setModuleText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const showModule = async () => {
    setError(null);
    const resp = await fetch("/api/honeypots/actions/responder-module");
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      setError(body.detail || `Failed (${resp.status})`);
      return;
    }
    const files = body.files || {};
    setModuleText(Object.entries(files).map(([path, text]) => `# ${path}\n${text}`).join("\n\n") + `\n\n# services.tf (root)\n${body.root_block || ""}`);
  };

  return (
    <div className="rounded-lg border border-gray-200 bg-white px-4 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0">
          <p className="text-xs font-semibold text-gray-700">Responder role (Terraform)</p>
          <p className="text-xs text-gray-500">Preview the IAM role generated from the enabled actions.</p>
        </div>
        <button onClick={showModule} className={`ml-auto ${BTN_OUTLINE}`}>
          Responder role (Terraform)
        </button>
      </div>
      {error && <div className="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      {moduleText !== null && (
        <div className="mt-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-gray-900">Responder role — generated from the enabled rows</h3>
            <button onClick={() => setModuleText(null)} className={`text-xs ${LINK_BTN}`}>
              Close
            </button>
          </div>
          <pre className="mt-2 max-h-96 overflow-auto rounded bg-gray-900 p-3 text-xs text-gray-100">{moduleText}</pre>
        </div>
      )}
    </div>
  );
}

/** Audit of the response actions taken, newest first; collapsible, 5 rows until "Show all". */
export function HoneypotAuditList({ defaultOpen = false }: { defaultOpen?: boolean }) {
  const [audits, setAudits] = useState<Audit[]>([]);
  const [open, setOpen] = useState(defaultOpen);
  const [all, setAll] = useState(false);

  useEffect(() => {
    fetch("/api/honeypots/audits?limit=50")
      .then((r) => (r.ok ? r.json() : []))
      .then((au: unknown) => {
        const list = Array.isArray(au) ? (au as Audit[]) : [];
        setAudits([...list].sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at)));
      })
      .catch(() => {});
  }, []);

  return (
    <section>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="inline-flex items-center gap-1.5 bg-transparent p-0 text-lg font-semibold text-gray-900 hover:bg-transparent hover:text-blue-700"
      >
        <span className="text-sm text-gray-400">{open ? "▾" : "▸"}</span>
        Audit — {audits.length} action{audits.length === 1 ? "" : "s"}
      </button>
      {open && (
        <div className="mt-2 overflow-x-auto rounded-lg border border-gray-200 bg-white">
          <table className="rt m-0 min-w-full text-sm shadow-none">
            <thead className="bg-gray-50 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-3 py-2">When</th>
                <th className="px-3 py-2">Action</th>
                <th className="px-3 py-2">Target</th>
                <th className="px-3 py-2">Chosen by</th>
                <th className="px-3 py-2">Result</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {audits.length === 0 && (
                <tr>
                  <td colSpan={5} className="rt-block px-3 py-4 text-center text-gray-500">
                    No actions taken yet.
                  </td>
                </tr>
              )}
              {(all ? audits : audits.slice(0, 5)).map((a) => {
                const target = a.target?.task_arn || a.target?.access_key_id;
                return (
                  <tr key={a.audit_id}>
                    <td data-label="When" className="whitespace-nowrap px-3 py-2 text-xs text-gray-600" title={exactUtc(a.started_at)}>{relativeTime(a.started_at)}</td>
                    <td data-label="Action" className="px-3 py-2">{a.action_type}</td>
                    <td data-label="Target" className="px-3 py-2 text-xs text-gray-700">{target ? <Short value={target} /> : "—"}</td>
                    <td data-label="Chosen by" className="px-3 py-2 text-xs">{a.chosen_by}</td>
                    <td data-label="Result" className="px-3 py-2">
                      <span className={`rounded px-2 py-0.5 text-xs ${STATUS_BADGE[a.status] || "bg-gray-100 text-gray-700"}`}>{a.status}</span>
                      {a.verify?.steady_after_s != null && <span className="ml-2 text-xs text-gray-500">steady in {a.verify.steady_after_s}s</span>}
                      {a.error && <span className="ml-2 text-xs text-red-600">{a.error}</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {audits.length > 5 && (
            <div className="border-t border-gray-100 bg-gray-50 px-3 py-2 text-xs">
              <button onClick={() => setAll((v) => !v)} className={LINK_BTN}>
                {all ? "Show last 5" : `Show all (${audits.length})`}
              </button>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

export default function HoneypotActions() {
  const readOnly = useReadOnly();
  const [rows, setRows] = useState<ActionRow[]>([]);
  const [catalog, setCatalog] = useState<CatalogEntry[]>([]);
  const [adding, setAdding] = useState(false);
  const [draftType, setDraftType] = useState("");
  const [draftScope, setDraftScope] = useState<Scope>(emptyScope());
  const [draftMode, setDraftMode] = useState<Mode>("approve");
  const [draftRisk, setDraftRisk] = useState(2);
  const [error, setError] = useState<string | null>(null);
  const [prNote, setPrNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const a = await fetch("/api/honeypots/actions").then((r) => r.json());
      setRows(Array.isArray(a.actions) ? a.actions : []);
      setCatalog(Array.isArray(a.catalog) ? a.catalog : []);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const selected = catalog.find((c) => c.action_type === draftType) || null;

  const submit = async () => {
    setError(null);
    const resp = await fetch("/api/honeypots/actions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action_type: draftType, scope: draftScope, mode: draftMode, risk_max: draftRisk, enabled: true }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      setError(body.detail || `Failed (${resp.status})`);
      return;
    }
    setAdding(false);
    setDraftType("");
    setDraftScope(emptyScope());
    await load();
  };

  const patch = async (row: ActionRow, body: Partial<Pick<ActionRow, "mode" | "risk_max" | "enabled">>) => {
    const resp = await fetch(`/api/honeypots/actions/${row.action_id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) setError(`Update failed (${resp.status})`);
    await load();
  };

  const remove = async (row: ActionRow) => {
    if (!confirm(`Remove "${row.catalog?.title || row.action_type}" from the whitelist?`)) return;
    await fetch(`/api/honeypots/actions/${row.action_id}`, { method: "DELETE" });
    await load();
  };

  const openPr = async () => {
    setError(null);
    const resp = await fetch("/api/honeypots/actions/responder-pr", { method: "POST" });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      setError(body.detail || `Failed (${resp.status})`);
      return;
    }
    setPrNote("Responder role PR queued; it appears in Open Requests when ready.");
  };

  return (
    <section>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">What Compass may do</h2>
          <p className="text-sm text-gray-600">
            When a honeytoken trips, the agent picks from these actions only. The responder role in your account is built from this list.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button onClick={openPr} {...writeProps(readOnly)} className={BTN_OUTLINE}>
            Open responder role PR
          </button>
          <button onClick={() => setAdding(true)} {...writeProps(readOnly)} className="rounded bg-gray-900 px-3 py-1.5 text-sm text-white hover:bg-gray-800 disabled:opacity-50">
            + Allow an action
          </button>
        </div>
      </div>

      {error && <div className="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      {prNote && <div className="mt-3 rounded border border-green-200 bg-green-50 px-3 py-2 text-sm text-green-800">{prNote}</div>}

      <div className="mt-4 overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="rt m-0 min-w-full text-sm shadow-none">
          <thead className="bg-gray-50 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
            <tr>
              <th className="px-3 py-2">Action</th>
              <th className="px-3 py-2">Scope</th>
              <th className="px-3 py-2">Mode</th>
              <th className="px-3 py-2">Risk max</th>
              <th className="px-3 py-2">Enabled</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {rows.length === 0 && (
              <tr>
                <td colSpan={6} className="rt-block px-3 py-6 text-center text-gray-500">
                  Nothing allowed yet. Compass will only recommend.
                </td>
              </tr>
            )}
            {rows.map((row) => (
              <tr key={row.action_id}>
                <td className="rt-block px-3 py-2">
                  <div className="font-medium text-gray-900">{row.catalog?.title || row.action_type}</div>
                  <div className="text-xs text-gray-500">{row.catalog?.description}</div>
                </td>
                <td data-label="Scope" className="px-3 py-2 text-xs text-gray-700">
                  <div className="min-w-0">
                    {(Object.keys(row.scope) as ScopeFields[])
                      .filter((k) => row.scope[k]?.length)
                      .map((k) => (
                        <div key={k}>
                          <span className="text-gray-500">{SCOPE_LABELS[k]}:</span>{" "}
                          <span className="break-all font-mono">{row.scope[k].join(", ")}</span>
                        </div>
                      ))}
                  </div>
                </td>
                <td data-label="Mode" className="px-3 py-2">
                  <select value={row.mode} onChange={(e) => patch(row, { mode: e.target.value as Mode })} {...writeProps(readOnly)} className="rounded border border-gray-300 px-2 py-1 text-sm">
                    {(Object.keys(MODE_HELP) as Mode[]).map((m) => (
                      <option key={m} value={m}>
                        {m} — {MODE_HELP[m]}
                      </option>
                    ))}
                  </select>
                </td>
                <td data-label="Risk max" className="px-3 py-2">
                  <input
                    type="number"
                    min={1}
                    max={5}
                    value={row.risk_max}
                    onChange={(e) => patch(row, { risk_max: Number(e.target.value) })}
                    {...writeProps(readOnly)}
                    className="w-16 rounded border border-gray-300 px-2 py-1 text-sm"
                  />
                </td>
                <td data-label="Enabled" className="px-3 py-2">
                  <input type="checkbox" checked={row.enabled} onChange={(e) => patch(row, { enabled: e.target.checked })} {...writeProps(readOnly)} />
                </td>
                <td className="rt-block px-3 py-2 text-right">
                  <button onClick={() => remove(row)} {...writeProps(readOnly)} className={BTN_QUIET_DANGER_SM}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {adding && (
        <div className="mt-4 rounded-lg border border-gray-200 bg-gray-50 p-4">
          <div className="grid gap-3 md:grid-cols-2">
            <label className="text-sm">
              <span className="block text-xs font-semibold text-gray-600">Action</span>
              <select value={draftType} onChange={(e) => setDraftType(e.target.value)} className="mt-1 w-full rounded border border-gray-300 px-2 py-1">
                <option value="">Choose…</option>
                {catalog.map((c) => (
                  <option key={c.action_type} value={c.action_type}>
                    {c.title}
                  </option>
                ))}
              </select>
              {selected && <span className="mt-1 block text-xs text-gray-500">{selected.description}</span>}
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="text-sm">
                <span className="block text-xs font-semibold text-gray-600">Mode</span>
                <select value={draftMode} onChange={(e) => setDraftMode(e.target.value as Mode)} className="mt-1 w-full rounded border border-gray-300 px-2 py-1">
                  {(Object.keys(MODE_HELP) as Mode[]).map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </label>
              <label className="text-sm">
                <span className="block text-xs font-semibold text-gray-600">Risk max (1–5)</span>
                <input type="number" min={1} max={5} value={draftRisk} onChange={(e) => setDraftRisk(Number(e.target.value))} className="mt-1 w-full rounded border border-gray-300 px-2 py-1" />
              </label>
            </div>
            {selected?.scope_fields.map((field) => (
              <label key={field} className="text-sm md:col-span-2">
                <span className="block text-xs font-semibold text-gray-600">{SCOPE_LABELS[field]} (one per line)</span>
                <textarea
                  rows={2}
                  value={draftScope[field].join("\n")}
                  onChange={(e) => setDraftScope({ ...draftScope, [field]: e.target.value.split("\n").map((s) => s.trim()).filter(Boolean) })}
                  className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono text-xs"
                />
              </label>
            ))}
          </div>
          <div className="mt-3 flex gap-2">
            <button onClick={submit} {...writeProps(readOnly, !draftType)} className="rounded bg-gray-900 px-3 py-1.5 text-sm text-white disabled:opacity-50">
              Allow
            </button>
            <button onClick={() => setAdding(false)} className={BTN_OUTLINE}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
