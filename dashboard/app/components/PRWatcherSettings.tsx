"use client";

import { useCallback, useEffect, useState } from "react";

/**
 * Settings → Pull requests: how far Compass goes on its own pull requests.
 * Mirrors the Auto-resolve card: pills, saved on click.
 */

type Settings = {
  mode: "off" | "approve" | "autopilot";
  risk_max: number;
  max_revisions: number;
  reply: boolean;
  auto_merge: { enabled: boolean; allow_types: string[]; repos: string[]; risk_max: number; fix_rounds: number };
};

const MODES: Array<{ value: Settings["mode"]; label: string; help: string }> = [
  { value: "off", label: "Off", help: "Track only. No replies, no pushes." },
  { value: "approve", label: "Approve", help: "Compass plans each revision and waits for you to approve it." },
  { value: "autopilot", label: "Autopilot", help: "Compass pushes revisions up to the risk level below; anything riskier waits for you." },
];

const RISK: Array<{ value: number; label: string; cls: string }> = [
  { value: 0, label: "Off", cls: "bg-gray-200 text-gray-700 border border-gray-300" },
  { value: 1, label: "1 · Safe", cls: "bg-emerald-100 text-emerald-800 border border-emerald-200" },
  { value: 2, label: "2 · Low", cls: "bg-green-100 text-green-800 border border-green-200" },
  { value: 3, label: "3 · Medium", cls: "bg-yellow-100 text-yellow-800 border border-yellow-200" },
  { value: 4, label: "4 · High", cls: "bg-orange-100 text-orange-800 border border-orange-200" },
  { value: 5, label: "5 · Critical", cls: "bg-red-100 text-red-800 border border-red-200" },
];

function Pills<T extends string | number>({ options, value, onPick, disabled }: { options: Array<{ value: T; label: string; cls?: string }>; value: T; onPick: (v: T) => void; disabled: boolean }) {
  return (
    <div className="flex flex-wrap gap-2">
      {options.map((o) => {
        const selected = o.value === value;
        return (
          <button
            key={String(o.value)}
            type="button"
            disabled={disabled}
            onClick={() => onPick(o.value)}
            aria-pressed={selected}
            className={`px-3 py-1 text-xs font-semibold rounded-full transition focus:outline-none focus:ring-2 focus:ring-offset-1 focus:ring-gray-700 ${o.cls ?? "bg-gray-100 text-gray-800 border border-gray-200"} ${selected ? "ring-2 ring-gray-800 ring-offset-1" : "opacity-60 hover:opacity-100"} ${disabled ? "cursor-not-allowed opacity-50" : ""}`}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export default function PRWatcherSettings({ readOnly }: { readOnly: boolean }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [repos, setRepos] = useState("");

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/prs/watcher");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as Settings;
      setSettings(data);
      setRepos((data.auto_merge?.repos ?? []).join(", "));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load pull request settings");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (patch: Record<string, unknown>) => {
    setSaving(true);
    try {
      const res = await fetch("/api/prs/watcher", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
      setSettings(data as Settings);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  const disabled = readOnly || saving || !settings;
  const mode = settings?.mode ?? "autopilot";

  return (
    <section className="bg-white border rounded-lg shadow-sm p-6 space-y-4">
      <div className="max-w-xl space-y-2">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold">Pull requests</h2>
          <span className="text-xs text-gray-500 cursor-help" title="Compass watches the pull requests it opened, answers reviewers and pushes revisions that keep the original change. The intent guard runs before every push regardless of these settings.">?</span>
        </div>
        <p className="text-sm text-gray-600">How far Compass goes on its own pull requests once reviews come in. Nothing here can make it merge, force-push or push over a person's commit.</p>
      </div>

      {error && <div className="text-xs text-red-600">{error}</div>}

      <div className="grid gap-5 md:grid-cols-2">
        <div className="space-y-2">
          <div className="text-sm font-medium text-gray-800">Mode</div>
          <Pills options={MODES.map((m) => ({ value: m.value, label: m.label }))} value={mode} onPick={(v) => void save({ mode: v })} disabled={disabled} />
          <p className="text-xs text-gray-500">{MODES.find((m) => m.value === mode)?.help}</p>
        </div>

        <div className="space-y-2">
          <div className="text-sm font-medium text-gray-800">Push revisions up to risk</div>
          <Pills options={RISK} value={settings?.risk_max ?? 2} onPick={(v) => void save({ risk_max: v })} disabled={disabled || mode !== "autopilot"} />
          <p className="text-xs text-gray-500">Revision risk = the finding's risk, +1 outside the original files, +1 on IAM, network, KMS or production paths.</p>
        </div>

        <div className="space-y-2">
          <div className="text-sm font-medium text-gray-800">Max revisions per pull request</div>
          <Pills options={[1, 2, 3, 4, 5].map((n) => ({ value: n, label: String(n) }))} value={settings?.max_revisions ?? 3} onPick={(v) => void save({ max_revisions: v })} disabled={disabled} />
          <p className="text-xs text-gray-500">After the cap the pull request is flagged for a person. Revisions do not consume fix credits.</p>
        </div>

        <div className="space-y-2">
          <div className="text-sm font-medium text-gray-800">Reply to reviewers</div>
          <Pills options={[{ value: "on", label: "On" }, { value: "off", label: "Off" }]} value={settings?.reply === false ? "off" : "on"} onPick={(v) => void save({ reply: v === "on" })} disabled={disabled} />
          <p className="text-xs text-gray-500">Answers questions, explains refusals and notes what each push addressed. Off still flags pull requests in the dashboard.</p>
        </div>
      </div>

      <div className="border-t pt-4 space-y-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <div className="text-sm font-medium text-gray-800">Auto-merge</div>
            <p className="text-xs text-gray-500">Only Terraform import pull requests, only in the repositories listed, only when GitHub's own rules and every Compass check pass. Needs <code>checks: write</code> on the GitHub App and the <code>compass-guardrails</code> ruleset on the repository.</p>
          </div>
          <Pills options={[{ value: "off", label: "Off" }, { value: "on", label: "On" }]} value={settings?.auto_merge?.enabled ? "on" : "off"} onPick={(v) => void save({ auto_merge: { enabled: v === "on" } })} disabled={disabled} />
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-2">
            <div className="text-xs font-medium text-gray-700">Merge up to risk</div>
            <Pills options={RISK.slice(1)} value={settings?.auto_merge?.risk_max ?? 1} onPick={(v) => void save({ auto_merge: { risk_max: v } })} disabled={disabled || !settings?.auto_merge?.enabled} />
          </div>
          <div className="space-y-2">
            <div className="text-xs font-medium text-gray-700">Repositories (owner/name, comma separated)</div>
            <input
              className="w-full rounded border border-gray-300 px-2 py-1 text-sm disabled:bg-gray-100"
              value={repos}
              disabled={disabled || !settings?.auto_merge?.enabled}
              onChange={(e) => setRepos(e.target.value)}
              onBlur={() => void save({ auto_merge: { repos: repos.split(",").map((r) => r.trim()).filter(Boolean) } })}
              placeholder="acme/infra"
            />
          </div>
        </div>
      </div>
      <div className="text-xs text-gray-600">{saving ? "Saving..." : settings ? "Saved" : "Loading..."}</div>
    </section>
  );
}
