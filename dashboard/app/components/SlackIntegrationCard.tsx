"use client";

import React, { useCallback, useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { exactUtc, relativeTime, writeProps } from "./honeypotFormat";

// Slack is a workspace-level integration: connected here (global Settings),
// while Honeypots → Settings only picks the channels alerts go to.

export type SlackStatus = {
  available: boolean;
  connected: boolean;
  team_id?: string | null;
  team_name?: string | null;
  connected_at?: string | null;
};

export const SLACK_INSTALL_URL = "/api/integrations/slack/install";
export const SLACK_SETTINGS_HREF = "/workspace?tab=settings";

export function SlackIcon({ className = "h-4 w-4 mr-2 shrink-0" }: { className?: string }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className={className}>
      <rect x="10" y="2" width="4" height="9" rx="2" fill="#36C5F0" />
      <rect x="13" y="10" width="9" height="4" rx="2" fill="#2EB67D" />
      <rect x="10" y="13" width="4" height="9" rx="2" fill="#ECB22E" />
      <rect x="2" y="10" width="9" height="4" rx="2" fill="#E01E5A" />
    </svg>
  );
}

/** `/api/integrations/slack/status`; `null` while loading. Unavailable on any error. */
export function useSlackStatus(): [SlackStatus | null, () => void] {
  const [status, setStatus] = useState<SlackStatus | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let cancelled = false;
    fetch("/api/integrations/slack/status")
      .then((res) => (res.ok ? res.json() : { available: false, connected: false }))
      .then((d: SlackStatus) => { if (!cancelled) setStatus({ ...d, available: Boolean(d.available), connected: Boolean(d.connected) }); })
      .catch(() => { if (!cancelled) setStatus({ available: false, connected: false }); });
    return () => { cancelled = true; };
  }, [tick]);
  return [status, useCallback(() => setTick((t) => t + 1), [])];
}

// Reasons the callback can send back (?slack=error&reason=…).
const SLACK_ERROR_TEXT: Record<string, string> = {
  access_denied: "Slack install was cancelled.",
  invalid_state: "The Slack sign-in link was not valid. Try Connect Slack again.",
  expired_state: "The Slack sign-in link expired. Try Connect Slack again.",
  workspace_mismatch: "The Slack sign-in was started from another workspace.",
  read_only: "This environment is read-only.",
  not_configured: "The Slack app is not configured for this installation.",
  storage_failed: "Slack approved the install, but the connection could not be saved. Try again.",
};

/** One-shot notice from the OAuth redirect; the query params are removed once read. */
function useSlackConnectNotice(): [{ ok: boolean; text: string } | null, () => void] {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => {
    const outcome = searchParams.get("slack");
    if (outcome !== "connected" && outcome !== "error") return;
    const reason = searchParams.get("reason") || "";
    setNotice(
      outcome === "connected"
        ? { ok: true, text: "Slack connected. Pick alert channels in Honeypots → Settings." }
        : { ok: false, text: SLACK_ERROR_TEXT[reason] ?? `Slack connection failed${reason ? ` (${reason})` : ""}.` },
    );
    const rest = new URLSearchParams(searchParams.toString());
    rest.delete("slack");
    rest.delete("reason");
    const qs = rest.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  }, [searchParams, router, pathname]);
  return [notice, () => setNotice(null)];
}

const BTN = "inline-flex items-center justify-center rounded-md px-4 py-2 text-sm font-semibold border border-solid transition disabled:opacity-50 disabled:cursor-not-allowed";

export default function SlackIntegrationCard({ readOnly = false }: { readOnly?: boolean }) {
  const [status, refresh] = useSlackStatus();
  const [notice, dismissNotice] = useSlackConnectNotice();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A full navigation: the API redirects to Slack, and Slack back to the callback.
  const connect = () => { window.location.href = SLACK_INSTALL_URL; };

  const disconnect = async () => {
    if (!window.confirm("Disconnect Slack? Alerts stop going to Slack channels until you connect it again.")) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/integrations/slack", { method: "DELETE" });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || `HTTP ${res.status}`);
      }
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not disconnect Slack");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-label="Slack" className="bg-white border rounded-lg shadow-sm p-6 space-y-4">
      <div className="space-y-1">
        <h2 className="text-lg font-semibold flex items-center"><SlackIcon className="h-5 w-5 mr-2 shrink-0" />Slack</h2>
        <p className="text-sm text-gray-600">Send alerts and incident updates to Slack.</p>
      </div>

      {notice && (
        <div
          role="status"
          className={`flex items-start justify-between gap-2 rounded border px-3 py-2 text-sm ${notice.ok ? "border-green-200 bg-green-50 text-green-800" : "border-red-200 bg-red-50 text-red-700"}`}
        >
          <span>{notice.text}</span>
          <button onClick={dismissNotice} aria-label="Dismiss" className="bg-transparent hover:bg-transparent px-2 py-0 text-gray-400 hover:text-gray-600">&times;</button>
        </div>
      )}
      {error && <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}

      <div className="rounded border border-gray-200 bg-gray-50 px-4 py-3 text-sm text-gray-700">
        {status === null ? (
          <div>Checking Slack…</div>
        ) : status.connected ? (
          <div className="space-y-1">
            <div>
              Connected to <span className="font-semibold text-gray-900">{status.team_name || status.team_id || "Slack"}</span>
            </div>
            {status.connected_at && (
              <div className="text-xs text-gray-500" title={exactUtc(status.connected_at)}>
                Connected {relativeTime(status.connected_at)}
              </div>
            )}
          </div>
        ) : !status.available ? (
          <div className="text-gray-500">Slack app not configured for this installation</div>
        ) : (
          <div>Slack is not connected. Connect it, then pick alert channels in Honeypots → Settings.</div>
        )}
      </div>

      {/* A connection can exist without the OAuth app (a bot token set by an operator):
          it can still be disconnected; Connect / Reconnect need the app. */}
      {(status?.available || status?.connected) && (
        <div className="flex flex-wrap justify-end gap-2">
          {status.connected ? (
            <>
              {status.available && (
                <button type="button" onClick={connect} {...writeProps(readOnly, busy)} className={`${BTN} border-gray-300 bg-white text-gray-900 hover:bg-gray-50`}>
                  Reconnect
                </button>
              )}
              <button type="button" onClick={() => void disconnect()} {...writeProps(readOnly, busy)} className={`${BTN} border-red-200 bg-white text-red-700 hover:bg-red-50`}>
                {busy ? "Disconnecting…" : "Disconnect"}
              </button>
            </>
          ) : (
            <button type="button" onClick={connect} {...writeProps(readOnly)} className={`${BTN} border-gray-300 bg-white text-gray-900 shadow-sm hover:bg-gray-50`}>
              <SlackIcon />
              Connect Slack
            </button>
          )}
        </div>
      )}
    </section>
  );
}
