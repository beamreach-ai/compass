"use client";
// Settings → Captains: every captain this install carries, on or off, with its
// tier, state and configuration (docs/captain_extensions_spec.md §2.5). The
// rows come from /api/captains; a toggle that needs a dependency switched too
// asks first and retries with the flag the API wants.
import axios from "axios";
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { openSettingsComponents } from "../lib/captains";
import { productSettingsComponents } from "../lib/extensions";
import { useShell } from "../lib/shell";
import type { CaptainSummary } from "../lib/shell";
import CaptainCatalog, { RestartBanner } from "./CaptainCatalog";
import type { ExtensionsState } from "./CaptainCatalog";
import SchemaForm from "./SchemaForm";

type Listing = {
  core_version: string;
  licence?: { plan: string; captains: string[]; tiers: string[] };
  captains: CaptainSummary[];
  incompatible?: Record<string, string>;
  broken?: Record<string, string>;
  extensions?: ExtensionsState;
};

const TIER_BADGE: Record<string, string> = {
  open: "bg-emerald-50 text-emerald-700 border-emerald-200",
  cloud: "bg-sky-50 text-sky-700 border-sky-200",
  enterprise: "bg-violet-50 text-violet-700 border-violet-200",
};

const reasonText = (c: CaptainSummary, incompatible?: Record<string, string>): string | null => {
  switch (c.reason) {
    case "incompatible":
      return `Needs Compass ${c.requires_core}${incompatible?.[c.name] ? ` (${incompatible[c.name]})` : ""}.`;
    case "unlicensed":
      return "Not included in this workspace's licence.";
    case "disabled_by_deploy":
      return "Disabled for this environment by its operator.";
    case "disabled_by_admin":
      return "Turned off.";
    case "dependency_off":
      return `Needs ${c.blocked_by.join(", ")} turned on.`;
    default:
      return null;
  }
};

export default function CaptainsSection({ readOnly }: { readOnly: boolean }) {
  const { refreshSettingsBootstrap, settingsBootstrap } = useShell();
  const [listing, setListing] = useState<Listing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [view, setView] = useState<"installed" | "available">("installed");

  const load = useCallback(async () => {
    try {
      const res = await axios.get("/api/captains");
      setListing(res.data as Listing);
      setError(null);
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || "Failed to load captains");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const afterChange = useCallback(async () => {
    await load();
    await refreshSettingsBootstrap();
    window.dispatchEvent(new Event("beamreach:target-updated"));
  }, [load, refreshSettingsBootstrap]);

  const toggle = useCallback(
    async (c: CaptainSummary, enabled: boolean) => {
      setPending(c.name);
      setError(null);
      const verb = enabled ? "enable" : "disable";
      const attempt = async (body: Record<string, boolean>) => axios.post(`/api/captains/${encodeURIComponent(c.name)}/${verb}`, body);
      try {
        try {
          await attempt({});
        } catch (e: any) {
          const body = e?.response?.data;
          const names: string[] = Array.isArray(body?.captains) ? body.captains : [];
          if (body?.code === "dependency_off" && window.confirm(`${c.label} needs ${names.join(", ")} turned on. Turn ${names.length > 1 ? "them" : "it"} on too?`)) {
            await attempt({ with_dependencies: true });
          } else if (body?.code === "required_by" && window.confirm(`${names.join(", ")} depend${names.length > 1 ? "" : "s"} on ${c.label}. Turn ${names.length > 1 ? "them" : "it"} off too?`)) {
            await attempt({ cascade: true });
          } else {
            throw e;
          }
        }
        await afterChange();
      } catch (e: any) {
        const body = e?.response?.data;
        if (body?.code !== "dependency_off" && body?.code !== "required_by") {
          setError((typeof body?.detail === "string" && body.detail) || e?.message || `Failed to ${verb} ${c.label}`);
        }
      } finally {
        setPending(null);
      }
    },
    [afterChange],
  );

  const uninstall = useCallback(
    async (c: CaptainSummary) => {
      if (!window.confirm(`Uninstall ${c.label}? It goes at the next restart; its data and settings stay.`)) return;
      setPending(c.name);
      setError(null);
      try {
        await axios.post("/api/captains/uninstall", { name: c.name });
        await load();
      } catch (e: any) {
        setError(e?.response?.data?.detail || e?.message || `Failed to uninstall ${c.label}`);
      } finally {
        setPending(null);
      }
    },
    [load],
  );

  const settingsComponents = useMemo(() => ({ ...openSettingsComponents, ...productSettingsComponents }), []);
  const rows = (listing?.captains || []).filter((c) => !c.core);
  const upgradeUrl = settingsBootstrap?.billing?.upgrade_url || null;
  const contact = settingsBootstrap?.billing?.enterprise_contact_email || null;

  return (
    <section className="bg-white border rounded-lg shadow-sm p-6 space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="space-y-1">
          <h2 className="text-lg font-semibold">Captains</h2>
          <p className="text-sm text-gray-600">What this Compass can do. Turn a captain off and its tab goes, its work is refused, nothing is deleted.</p>
        </div>
        {listing && <div className="text-xs text-gray-500">Compass {listing.core_version}{listing.licence ? ` · ${listing.licence.plan} plan` : ""}</div>}
      </div>
      <RestartBanner state={listing?.extensions || null} readOnly={readOnly} onRestarted={() => void load()} />
      <div className="flex gap-1 border-b border-gray-200 text-sm" role="tablist">
        {(["installed", "available"] as const).map((key) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={view === key}
            onClick={() => setView(key)}
            className={`-mb-px border-b-2 px-3 py-1.5 font-medium ${view === key ? "border-gray-900 text-gray-900" : "border-transparent text-gray-500 hover:text-gray-700"}`}
          >
            {key === "installed" ? `Installed${listing ? ` (${rows.length})` : ""}` : "Available"}
          </button>
        ))}
      </div>
      {view === "available" ? (
        <CaptainCatalog readOnly={readOnly} onChanged={() => void load()} />
      ) : (
      <>
      {error && <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      {!listing && !error && <div className="text-sm text-gray-500">Loading captains…</div>}
      <ul className="divide-y divide-gray-200 rounded border border-gray-200">
        {rows.map((c) => {
          const reason = reasonText(c, listing?.incompatible);
          const canToggle = !readOnly && c.installed && c.compatible && (c.licensed || !c.enabled) && c.reason !== "disabled_by_deploy";
          const Custom = c.settings_component ? settingsComponents[c.settings_component] : undefined;
          const configurable = Boolean(Custom) || c.has_config;
          const isOpen = open === c.name;
          return (
            <li key={c.name} className="px-4 py-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-semibold text-gray-900">{c.label}</span>
                    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${TIER_BADGE[c.tier] || TIER_BADGE.open}`}>{c.tier}</span>
                    <span className="text-[11px] text-gray-400">v{c.version}</span>
                    <span className={`inline-flex items-center rounded px-2 py-0.5 text-[11px] font-semibold ${c.active ? "bg-green-100 text-green-800" : "bg-gray-100 text-gray-600"}`}>{c.active ? "On" : "Off"}</span>
                  </div>
                  <p className="mt-1 text-sm text-gray-600">{c.summary}</p>
                  {reason && <p className="mt-1 text-xs text-amber-700">{reason}</p>}
                  {c.depends_on.length > 0 && <p className="mt-1 text-xs text-gray-400">Needs: {c.depends_on.join(", ")}</p>}
                  {!c.licensed && (upgradeUrl || contact) && (
                    <p className="mt-1 text-xs">
                      {upgradeUrl ? <a href={upgradeUrl} className="text-blue-600 underline">Get a licence</a> : <a href={`mailto:${contact}`} className="text-blue-600 underline">Ask about a licence</a>}
                    </p>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {c.installed_from === "page" && !readOnly && (
                    <button type="button" onClick={() => uninstall(c)} disabled={pending === c.name} className="rounded border border-gray-300 px-3 py-1 text-xs font-semibold text-gray-700 hover:bg-gray-50 disabled:opacity-50">
                      Uninstall
                    </button>
                  )}
                  {configurable && (
                    <button type="button" onClick={() => setOpen(isOpen ? null : c.name)} className="rounded border border-gray-300 px-3 py-1 text-xs font-semibold text-gray-700 hover:bg-gray-50">
                      {isOpen ? "Close" : "Configure"}
                    </button>
                  )}
                  <button
                    type="button"
                    disabled={!canToggle || pending === c.name}
                    onClick={() => toggle(c, !c.enabled)}
                    className={`rounded px-3 py-1 text-xs font-semibold transition disabled:cursor-not-allowed disabled:opacity-50 ${c.enabled ? "border border-gray-300 text-gray-700 hover:bg-gray-50" : "bg-gray-900 text-white hover:opacity-90"}`}
                    title={!c.licensed && !c.enabled ? "Needs a licence" : undefined}
                  >
                    {pending === c.name ? "…" : c.enabled ? "Turn off" : "Turn on"}
                  </button>
                </div>
              </div>
              {isOpen && (
                <div className="mt-4 rounded border border-gray-100 bg-gray-50 p-4">
                  {Custom ? <Custom readOnly={readOnly} /> : <SchemaForm captain={c.name} readOnly={readOnly} onSaved={() => void refreshSettingsBootstrap()} />}
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {listing && Object.keys(listing.broken || {}).length > 0 && (
        <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          Failed to load: {Object.entries(listing.broken || {}).map(([ref, why]) => `${ref} (${why})`).join("; ")}
        </div>
      )}
      </>
      )}
    </section>
  );
}
