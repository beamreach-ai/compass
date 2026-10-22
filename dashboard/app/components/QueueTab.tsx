"use client";
// The Queue tab: recent jobs across every captain, with their timelines and a
// restart. The shell owns the job list and its polling (useShell().queueJobs).
import axios from "axios";
import React, { useCallback, useState } from "react";
import { useSearchParams } from "next/navigation";
import InboxFeed from "./InboxFeed";
import { formatTimestamp } from "../lib/format";
import { useShell } from "../lib/shell";

const normalizeQueueStatus = (status: string) => (status || "").toLowerCase();

const getQueueStatusBadge = (status: string) => {
  const normalized = normalizeQueueStatus(status);
  if (normalized === "completed") return "bg-green-100 text-green-800 border border-green-200";
  if (normalized === "failed" || normalized === "error") return "bg-red-100 text-red-800 border border-red-200";
  if (normalized === "running" || normalized === "in_progress") return "bg-blue-100 text-blue-800 border border-blue-200";
  if (normalized === "queued" || normalized === "pending") return "bg-yellow-100 text-yellow-800 border border-yellow-200";
  return "bg-gray-100 text-gray-700 border border-gray-200";
};

const isQueueJobRestartable = (status: string) => {
  const normalized = normalizeQueueStatus(status);
  return ["completed", "failed", "error", "skipped", "cancelled", "canceled"].includes(normalized);
};

// Triage and fix jobs wait for the infra scan (the product's restart guard says the same).
const SCAN_GATED_CAPTAINS = ["engine", "finops_engine", "triage", "finops_triage"];

export default function QueueTab() {
  const { queueJobs, queueError, fetchQueueJobs, infraScan, infraScanBlocked } = useShell();
  const view = useSearchParams()?.get("view") || "jobs";
  const [restartPending, setRestartPending] = useState<string | null>(null);
  const [restartError, setRestartError] = useState<string | null>(null);

  const restartQueueJob = useCallback(
    async (jobId: string) => {
      if (!jobId) return;
      setRestartPending(jobId);
      try {
        await axios.post(`/api/jobs/${encodeURIComponent(jobId)}/restart`);
        await fetchQueueJobs();
        setRestartError(null);
      } catch (error: any) {
        console.error("Failed to restart job", error);
        setRestartError(error?.response?.data?.detail || "Failed to restart job");
      } finally {
        setRestartPending(null);
      }
    },
    [fetchQueueJobs],
  );

  const error = restartError || queueError;

  if (view === "needs_you") {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold">Queue</h1>
        <InboxFeed tab="queue" empty="Nothing needs you here. A failed or dead job would be listed until a newer run of the same command completes." />
      </div>
    );
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-4">Queue</h1>
      <p className="text-sm text-gray-600">
        Recent captain jobs across all workers. This view refreshes automatically every few seconds while open.
      </p>
      {error && (
        <div className="mt-4 rounded border border-red-200 bg-red-50 text-red-700 px-4 py-2 text-sm">{error}</div>
      )}
      <div className="mt-6 overflow-x-auto rounded border border-gray-200">
        <table className="min-w-full divide-y divide-gray-200 text-sm">
          <thead className="bg-gray-100 text-left">
            <tr>
              <th className="px-4 py-3 font-semibold text-gray-700">Job</th>
              <th className="px-4 py-3 font-semibold text-gray-700">Captain</th>
              <th className="px-4 py-3 font-semibold text-gray-700">Command</th>
              <th className="px-4 py-3 font-semibold text-gray-700">Status</th>
              <th className="px-4 py-3 font-semibold text-gray-700">Queue</th>
              <th className="px-4 py-3 font-semibold text-gray-700">Timestamps</th>
              <th className="px-4 py-3 font-semibold text-gray-700 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-200 bg-white">
            {queueJobs.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-4 py-6 text-center text-gray-500">
                  No jobs found yet.
                </td>
              </tr>
            ) : (
              queueJobs.map((job) => {
                const statusLabel = job.status || "queued";
                const scanBlockedJobRestart = infraScanBlocked && SCAN_GATED_CAPTAINS.includes(job.captain || "");
                const canRestart = isQueueJobRestartable(statusLabel) && !scanBlockedJobRestart;
                const isRestarting = restartPending === job.job_id;
                return (
                  <React.Fragment key={job.job_id}>
                    <tr className="align-top">
                      <td className="px-4 py-3">
                        <div className="font-mono text-xs break-all">{job.job_id}</div>
                        {job.session_id && <div className="mt-1 text-xs text-gray-500 break-all">Session: {job.session_id}</div>}
                      </td>
                      <td className="px-4 py-3 capitalize">{job.captain}</td>
                      <td className="px-4 py-3 break-words">{job.command}</td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex items-center rounded px-2 py-1 text-xs font-semibold ${getQueueStatusBadge(statusLabel)}`}>
                          {statusLabel}
                        </span>
                        {job.error && <div className="mt-2 text-xs text-red-600 break-words">{job.error}</div>}
                      </td>
                      <td className="px-4 py-3 text-xs text-gray-600 break-words">{job.queue}</td>
                      <td className="px-4 py-3 text-xs text-gray-600">
                        <div>
                          Updated: <span className="font-semibold text-gray-800">{formatTimestamp(job.updated_at)}</span>
                        </div>
                        <div className="mt-1">
                          Created: <span className="text-gray-700">{formatTimestamp(job.created_at)}</span>
                        </div>
                      </td>
                      <td className="px-4 py-3 text-right">
                        <button
                          onClick={() => restartQueueJob(job.job_id)}
                          disabled={!canRestart || isRestarting}
                          className={`inline-flex items-center justify-center rounded border px-3 py-1 text-xs font-semibold transition ${
                            canRestart ? "border-gray-900 text-gray-900 hover:bg-gray-900 hover:text-white" : "border-gray-200 text-gray-400 cursor-not-allowed"
                          } ${isRestarting ? "opacity-60 cursor-wait" : ""}`}
                          title={
                            canRestart
                              ? "Re-enqueue this job"
                              : scanBlockedJobRestart
                                ? infraScan?.disabled_reason || "Complete the infra scan before restarting triage or fix jobs."
                                : "Restart available when job is finished"
                          }
                        >
                          {isRestarting ? "Restarting…" : "Restart"}
                        </button>
                      </td>
                    </tr>
                    {job.events.length > 0 && (
                      <tr>
                        <td colSpan={7} className="bg-gray-50 px-4 py-3 text-xs text-gray-700">
                          <details open={statusLabel === "running"}>
                            <summary className="cursor-pointer select-none text-gray-600">Timeline ({job.events.length})</summary>
                            <ul className="mt-2 space-y-2">
                              {job.events.map((event, index) => (
                                <li key={`${event.timestamp}-${index}`} className="rounded border border-gray-200 bg-white px-3 py-2 shadow-sm">
                                  <div className="flex flex-wrap items-center justify-between gap-3">
                                    <span className="font-semibold text-gray-900">{formatTimestamp(event.timestamp)}</span>
                                    <span className="flex items-center gap-2 text-xs uppercase tracking-wide text-gray-500">
                                      {event.status && (
                                        <span className={`inline-flex items-center rounded px-2 py-0.5 font-semibold ${getQueueStatusBadge(event.status)}`}>
                                          {event.status}
                                        </span>
                                      )}
                                      {event.type && (!event.status || normalizeQueueStatus(event.status) !== normalizeQueueStatus(event.type)) && (
                                        <span>{event.type}</span>
                                      )}
                                    </span>
                                  </div>
                                  <div className="mt-1 text-gray-700">{event.message}</div>
                                </li>
                              ))}
                            </ul>
                          </details>
                        </td>
                      </tr>
                    )}
                    {job.result_summary && (
                      <tr>
                        <td colSpan={7} className="bg-gray-50 px-4 py-3 text-xs text-gray-700">
                          <details>
                            <summary className="cursor-pointer select-none text-gray-600">Result summary</summary>
                            <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words rounded border border-gray-200 bg-white p-3">
                              {JSON.stringify(job.result_summary, null, 2)}
                            </pre>
                          </details>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
