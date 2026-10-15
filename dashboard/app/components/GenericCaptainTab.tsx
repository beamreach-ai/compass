"use client";
// The page for a captain's tab when the dashboard build has no component for its
// key: a third-party captain installed into the open image. It shows what the
// manifest says, runs the captain's commands through POST /api/jobs with a JSON
// payload, and lists the captain's recent jobs from the shell's queue.
import axios from "axios";
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { formatTimestamp } from "../lib/format";
import { useShell } from "../lib/shell";
import type { CaptainSummary } from "../lib/shell";

type Detail = CaptainSummary & { commands?: Record<string, string> };

export default function GenericCaptainTab({ tabKey }: { tabKey: string }) {
  const { settingsBootstrap, queueJobs, fetchQueueJobs } = useShell();
  const summary = useMemo(
    () => (settingsBootstrap?.captains || []).find((c) => c.tabs.some((t) => t.key === tabKey)) || null,
    [settingsBootstrap?.captains, tabKey],
  );
  const [detail, setDetail] = useState<Detail | null>(null);
  const [command, setCommand] = useState<string>("");
  const [payload, setPayload] = useState<string>("{}");
  const [running, setRunning] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const readOnly = settingsBootstrap?.app?.read_only === true;

  const load = useCallback(async () => {
    if (!summary) return;
    try {
      const res = await axios.get(`/api/captains/${encodeURIComponent(summary.name)}`);
      setDetail(res.data as Detail);
      const names = Object.keys((res.data as Detail).commands || {});
      setCommand((current) => current || names[0] || "");
    } catch (e: any) {
      setMessage({ kind: "error", text: e?.response?.data?.detail || e?.message || "Failed to load the captain" });
    }
  }, [summary]);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async () => {
    if (!summary || !command) return;
    let parsed: unknown;
    try {
      parsed = payload.trim() ? JSON.parse(payload) : {};
    } catch {
      setMessage({ kind: "error", text: "The payload must be a JSON object." });
      return;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      setMessage({ kind: "error", text: "The payload must be a JSON object." });
      return;
    }
    setRunning(true);
    setMessage(null);
    try {
      const res = await axios.post("/api/jobs", { captain: summary.name, command, payload: parsed });
      const job = res.data || {};
      setMessage({ kind: "ok", text: `${command} ${job.status === "completed" ? "ran" : "queued"} (job ${job.job_id}).` });
      void fetchQueueJobs();
    } catch (e: any) {
      setMessage({ kind: "error", text: e?.response?.data?.detail || e?.message || `Failed to run ${command}` });
    } finally {
      setRunning(false);
    }
  };

  if (!summary) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-6 text-sm text-gray-600 shadow-sm">
        This tab is not available in this edition.
      </div>
    );
  }

  const commands = detail?.commands || {};
  const jobs = queueJobs.filter((j) => j.captain === summary.name).slice(0, 20);
  const tab = summary.tabs.find((t) => t.key === tabKey);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{tab?.label || summary.label}</h1>
        <p className="mt-1 text-sm text-gray-600">{summary.summary}</p>
        <p className="mt-1 text-xs text-gray-400">
          {summary.label} v{summary.version} · {summary.tier}
          {summary.has_config ? " · configured under Settings → Captains" : ""}
        </p>
      </div>

      <section className="bg-white border rounded-lg shadow-sm p-6 space-y-4">
        <h2 className="text-lg font-semibold">Run a command</h2>
        {Object.keys(commands).length === 0 ? (
          <p className="text-sm text-gray-500">This captain declares no commands.</p>
        ) : (
          <>
            <div className="grid gap-4 md:grid-cols-2">
              <label className="block text-sm">
                <span className="font-medium text-gray-900">Command</span>
                <select className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm" value={command} onChange={(e) => setCommand(e.target.value)} disabled={readOnly || running}>
                  {Object.entries(commands).map(([name, help]) => (
                    <option key={name} value={name}>{help ? `${name} — ${help}` : name}</option>
                  ))}
                </select>
              </label>
              <label className="block text-sm">
                <span className="font-medium text-gray-900">Payload (JSON)</span>
                <textarea className="mt-1 w-full rounded border border-gray-300 px-3 py-2 font-mono text-xs" rows={4} value={payload} onChange={(e) => setPayload(e.target.value)} disabled={readOnly || running} />
              </label>
            </div>
            {message && (
              <div className={`rounded border px-3 py-2 text-sm ${message.kind === "ok" ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{message.text}</div>
            )}
            {!readOnly && (
              <div className="flex justify-end">
                <button type="button" onClick={run} disabled={running || !command} className="rounded bg-gray-900 px-4 py-2 text-sm font-semibold text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50">
                  {running ? "Running…" : "Run"}
                </button>
              </div>
            )}
          </>
        )}
      </section>

      <section className="bg-white border rounded-lg shadow-sm p-6 space-y-3">
        <h2 className="text-lg font-semibold">Recent jobs</h2>
        {jobs.length === 0 ? (
          <p className="text-sm text-gray-500">No jobs yet.</p>
        ) : (
          <table className="min-w-full divide-y divide-gray-200 text-sm">
            <thead className="bg-gray-100 text-left">
              <tr>
                <th className="px-3 py-2 font-semibold text-gray-700">Command</th>
                <th className="px-3 py-2 font-semibold text-gray-700">Status</th>
                <th className="px-3 py-2 font-semibold text-gray-700">Updated</th>
                <th className="px-3 py-2 font-semibold text-gray-700">Result</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {jobs.map((job) => (
                <tr key={job.job_id} className="align-top">
                  <td className="px-3 py-2">{job.command}</td>
                  <td className="px-3 py-2">{job.status}{job.error ? <div className="text-xs text-red-600">{job.error}</div> : null}</td>
                  <td className="px-3 py-2 text-xs text-gray-600">{formatTimestamp(job.updated_at)}</td>
                  <td className="px-3 py-2 text-xs text-gray-600">
                    {job.result_summary ? <pre className="max-w-md overflow-x-auto whitespace-pre-wrap break-words">{JSON.stringify(job.result_summary, null, 1)}</pre> : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
