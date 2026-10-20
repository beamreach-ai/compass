"use client";
// Settings → Account → Access: the two roles, every grant with its state and pull request,
// the findings of the self-audit, and the two buttons (Verify now, Audit now). GET /api/access.
import axios from "axios";
import React, { useCallback, useEffect, useState } from "react";

type Grant = {
  grant_id: string;
  captain: string;
  action_type: string;
  title: string;
  change_type?: string | null;
  rollback_supported?: boolean | null;
  scope: Record<string, string[]>;
  mode: string;
  risk_max: number;
  grant_status: string | null;
  armed: boolean;
  grant_pr?: string | null;
  applied_at?: string | null;
  verified_at?: string | null;
  verification?: { simulated?: boolean | null; denied?: Array<{ action: string; resource?: string }> } | null;
  statements: Array<{ Action: string | string[]; Resource: string | string[] }>;
  accepted_by?: string | null;
};

type Finding = { finding_id: string; kind: string; role: string; detail: string; first_seen: string; statement?: unknown };

type Access = {
  connected: boolean;
  roles: { read?: string | null; act?: string | null };
  runtime_arn?: string | null;
  verified_at?: string | null;
  repository?: string | null;
  grants: Grant[];
  summary: { total: number; armed: number; in_flight: number; waiting_on: string[] };
  findings: Finding[];
  audit?: { at?: string; findings?: number } | null;
};

const STATE_BADGE: Record<string, string> = {
  armed: "bg-green-100 text-green-800",
  verified: "bg-green-50 text-green-700",
  applied: "bg-sky-100 text-sky-800",
  merged: "bg-sky-50 text-sky-700",
  pr_open: "bg-amber-100 text-amber-800",
  proposed: "bg-gray-100 text-gray-700",
  declined: "bg-gray-100 text-gray-500",
  disarmed: "bg-red-50 text-red-700",
  revoke_pr_open: "bg-red-50 text-red-700",
  revoke_merged: "bg-red-50 text-red-700",
  revoked: "bg-gray-100 text-gray-500",
};

const STATE_TEXT: Record<string, string> = {
  proposed: "proposed: accept it to open the pull request",
  pr_open: "pull request open: merge and apply it",
  merged: "merged, waiting for apply",
  applied: "applied: in the role, waiting for IAM's simulation",
  verified: "verified",
  armed: "armed: may run",
  declined: "declined",
  disarmed: "disarmed: permission still granted in AWS, removal waiting on its pull request",
  revoke_pr_open: "removal pull request open",
  revoke_merged: "removal merged, waiting for apply",
  revoked: "revoked",
};

function actionsOf(statements: Grant["statements"]): string[] {
  const out = new Set<string>();
  for (const s of statements || []) for (const a of Array.isArray(s.Action) ? s.Action : [s.Action]) out.add(a);
  return Array.from(out).sort();
}

export function AccessSection({ readOnly }: { readOnly?: boolean }) {
  const [access, setAccess] = useState<Access | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await axios.get("/api/access");
      setAccess(response.data as Access);
      setError(null);
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || "Could not load the access state");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (key: string, path: string, done: string) => {
    if (busy) return;
    setBusy(key);
    setError(null);
    setNote(null);
    try {
      await axios.post(path, {});
      setNote(done);
      await load();
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || "Failed");
    } finally {
      setBusy(null);
    }
  };

  if (!access) return <section className="bg-white border rounded-lg shadow-sm p-6 text-sm text-gray-500">{error || "Loading access…"}</section>;

  return (
    <section className="bg-white border rounded-lg shadow-sm p-6 space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h2 className="text-lg font-semibold">Access</h2>
          <p className="text-sm text-gray-600">
            What Compass may do in the account: every write is one grant, given and taken away by a pull request you merge.
            {access.summary.total > 0 ? ` ${access.summary.armed} of ${access.summary.total} actions armed.` : " No actions granted."}
          </p>
        </div>
        <div className="flex gap-2">
          <button type="button" disabled={Boolean(busy) || readOnly || !access.connected} onClick={() => act("verify", "/api/access/verify", "Verified against IAM.")}
            className="rounded border border-gray-300 bg-white px-3 py-1.5 text-sm font-semibold text-gray-700 hover:bg-gray-50 disabled:opacity-50">
            {busy === "verify" ? "Verifying…" : "Verify now"}
          </button>
          <button type="button" disabled={Boolean(busy) || readOnly || !access.connected} onClick={() => act("audit", "/api/access/audit", "Audit done.")}
            className="rounded border border-gray-300 bg-white px-3 py-1.5 text-sm font-semibold text-gray-700 hover:bg-gray-50 disabled:opacity-50">
            {busy === "audit" ? "Auditing…" : "Audit now"}
          </button>
          <button type="button" disabled={Boolean(busy) || readOnly || !access.repository} onClick={() => act("pr", "/api/access/pr", "The access pull request is queued; it appears under Pull requests.")}
            className="rounded bg-gray-900 px-3 py-1.5 text-sm font-semibold text-white hover:opacity-90 disabled:opacity-50">
            Open the access PR
          </button>
        </div>
      </div>
      {error && <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      {note && <div className="rounded border border-green-200 bg-green-50 px-3 py-2 text-sm text-green-700">{note}</div>}
      {!access.repository && (
        <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">Connect a repository (GitHub settings) to enable write actions: a grant is a pull request on it.</div>
      )}
      {access.findings.length > 0 && (
        <div className="rounded border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          <div className="font-semibold">{access.findings.length} finding{access.findings.length === 1 ? "" : "s"} from the self-audit</div>
          <ul className="mt-1 list-disc pl-5 text-xs">
            {access.findings.map((f) => (
              <li key={f.finding_id}>
                <span className="font-medium">{f.kind}</span> on the {f.role} role: {f.detail} <span className="text-amber-700">(first seen {f.first_seen})</span>
              </li>
            ))}
          </ul>
          <div className="mt-1 text-xs">The pull request regenerates the module, which removes what it does not contain.</div>
        </div>
      )}
      <div className="overflow-x-auto rounded-lg border border-gray-200">
        <table className="rt m-0 min-w-full text-sm shadow-none">
          <thead className="bg-gray-50 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
            <tr>
              <th className="px-3 py-2">Action</th>
              <th className="px-3 py-2">Allows</th>
              <th className="px-3 py-2">Mode</th>
              <th className="px-3 py-2">State</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {access.grants.length === 0 && (
              <tr>
                <td colSpan={5} className="rt-block px-3 py-6 text-center text-gray-500">No actions proposed yet. Compass only reads.</td>
              </tr>
            )}
            {access.grants.map((g) => {
              const state = g.grant_status || "proposed";
              return (
                <tr key={g.grant_id}>
                  <td className="rt-block px-3 py-2">
                    <div className="font-medium text-gray-900">{g.title}</div>
                    <div className="text-xs text-gray-500">{g.captain} · {g.action_type}{g.change_type ? ` · ${g.change_type}` : ""}{g.rollback_supported ? " · rollback" : ""}</div>
                  </td>
                  <td data-label="Allows" className="px-3 py-2 text-xs text-gray-700">
                    <div className="font-mono break-all">{actionsOf(g.statements).join(", ") || "nothing yet (name a scope)"}</div>
                    <div className="text-gray-500">
                      {Object.entries(g.scope || {}).filter(([, v]) => v && v.length).map(([k, v]) => `${k}: ${v.join(", ")}`).join(" · ") || "no scope"}
                    </div>
                  </td>
                  <td data-label="Mode" className="px-3 py-2 text-xs text-gray-700">{g.mode} · risk ≤ {g.risk_max}</td>
                  <td data-label="State" className="px-3 py-2 text-xs">
                    <span className={`inline-flex rounded-full px-2 py-0.5 font-semibold ${STATE_BADGE[state] || "bg-gray-100 text-gray-700"}`}>{state}</span>
                    <div className="mt-0.5 text-gray-500">{STATE_TEXT[state] || ""}</div>
                    {g.grant_pr && (
                      <a href={g.grant_pr} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">pull request ↗</a>
                    )}
                    {g.verification?.simulated === false && state === "armed" && <div className="text-gray-400">statements present; IAM's simulation was not available</div>}
                    {g.verification?.denied?.length ? <div className="text-red-700">denied: {g.verification.denied.map((d) => d.action).join(", ")}</div> : null}
                  </td>
                  <td className="rt-block px-3 py-2 text-right text-xs">
                    {state === "proposed" && (
                      <>
                        <button type="button" disabled={Boolean(busy) || readOnly} onClick={() => act(g.grant_id, `/api/access/grants/${g.grant_id}/accept`, "Accepted: the pull request is queued.")}
                          className="rounded bg-gray-900 px-2 py-1 font-semibold text-white hover:opacity-90 disabled:opacity-50">Accept</button>{" "}
                        <button type="button" disabled={Boolean(busy) || readOnly} onClick={() => act(g.grant_id, `/api/access/grants/${g.grant_id}/decline`, "Declined.")}
                          className="rounded border border-gray-300 px-2 py-1 font-semibold text-gray-700 hover:bg-gray-50 disabled:opacity-50">Decline</button>
                      </>
                    )}
                    {["pr_open", "merged", "applied", "verified", "armed"].includes(state) && (
                      <button type="button" disabled={Boolean(busy) || readOnly} onClick={() => act(g.grant_id, `/api/access/grants/${g.grant_id}/remove`, "Removed: disarmed now; the revoke pull request is queued.")}
                        className="rounded border border-red-200 px-2 py-1 font-semibold text-red-700 hover:bg-red-50 disabled:opacity-50">Remove</button>
                    )}
                    {["declined", "revoked"].includes(state) && (
                      <button type="button" disabled={Boolean(busy) || readOnly} onClick={() => act(g.grant_id, `/api/access/grants/${g.grant_id}/accept`, "Accepted again: the pull request is queued.")}
                        className="rounded border border-gray-300 px-2 py-1 font-semibold text-gray-700 hover:bg-gray-50 disabled:opacity-50">Grant again</button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="text-xs text-gray-500">
        Roles: <span className="font-mono">{access.roles.read || "none"}</span> (read) · <span className="font-mono">{access.roles.act || "none"}</span> (act)
        {access.audit?.at ? ` · last audit ${access.audit.at}` : " · never audited"}
      </div>
    </section>
  );
}
