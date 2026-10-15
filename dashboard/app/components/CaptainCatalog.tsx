"use client";
// Settings → Captains → Available: what the catalog offers (GET /api/captains/catalog),
// with each entry's status here. Installing is one line in .env and a restart
// (the open image installs COMPASS_EXTENSIONS on start); this view hands over
// that line. Paid captains without a public package link to their vendor.
import axios from "axios";
import React, { useCallback, useEffect, useState } from "react";

type CatalogEntry = {
  name: string;
  label: string;
  summary: string;
  tier: "open" | "cloud" | "enterprise";
  version: string;
  requires_core: string;
  author?: string | null;
  homepage?: string | null;
  source?: string | null;
  tags: string[];
  status: "installed" | "update" | "install" | "incompatible" | "vendor";
  installed_version?: string | null;
  licensed: boolean;
  env_line?: string | null;
};

type CatalogListing = {
  enabled: boolean;
  core_version: string;
  source?: string | null;
  error?: string | null;
  entries: CatalogEntry[];
};

const TIER_BADGE: Record<string, string> = {
  open: "bg-emerald-50 text-emerald-700 border-emerald-200",
  cloud: "bg-sky-50 text-sky-700 border-sky-200",
  enterprise: "bg-violet-50 text-violet-700 border-violet-200",
};

const STATUS: Record<CatalogEntry["status"], { label: string; cls: string }> = {
  installed: { label: "Installed", cls: "bg-green-100 text-green-800" },
  update: { label: "Update available", cls: "bg-amber-100 text-amber-800" },
  install: { label: "Available", cls: "bg-gray-100 text-gray-700" },
  incompatible: { label: "Needs a newer Compass", cls: "bg-gray-100 text-gray-500" },
  vendor: { label: "From its vendor", cls: "bg-sky-100 text-sky-800" },
};

function InstallSteps({ entry }: { entry: CatalogEntry }) {
  const [copied, setCopied] = useState(false);
  if (!entry.env_line) return null;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(entry.env_line || "");
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="mt-3 space-y-2 rounded border border-gray-100 bg-gray-50 p-3 text-xs text-gray-700">
      <div>
        {entry.status === "update" ? "To update, change its line" : "To install, add this line"} to the <code>.env</code> next to your <code>docker-compose.yml</code> (several extensions go on one line, space-separated):
      </div>
      <div className="flex items-start gap-2">
        <pre className="flex-1 overflow-x-auto whitespace-pre-wrap break-all rounded border border-gray-200 bg-white p-2 font-mono text-[11px]">{entry.env_line}</pre>
        <button type="button" onClick={copy} className="shrink-0 rounded border border-gray-300 bg-white px-2 py-1 text-[11px] font-semibold text-gray-700 hover:bg-gray-50">
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <div>
        Then restart: <code className="font-mono">docker compose up -d</code>. It appears under Installed, on by default.
      </div>
      {!entry.licensed && <div className="text-amber-700">This workspace&apos;s licence does not include it: it will install but stay off until it does.</div>}
    </div>
  );
}

export default function CaptainCatalog() {
  const [listing, setListing] = useState<CatalogListing | null>(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async (q: string, refresh = false) => {
    setLoading(true);
    try {
      const res = await axios.get("/api/captains/catalog", { params: { q: q || undefined, refresh: refresh || undefined } });
      setListing(res.data as CatalogListing);
      setError(null);
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || "Failed to load the catalog");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const handle = window.setTimeout(() => void load(query), query ? 250 : 0);
    return () => window.clearTimeout(handle);
  }, [load, query]);

  if (listing && !listing.enabled) {
    return <div className="text-sm text-gray-500">The captain catalog is turned off for this install (COMPASS_CATALOG_URL=off).</div>;
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search captains"
          className="min-w-0 flex-1 rounded border border-gray-300 px-3 py-1.5 text-sm focus:border-gray-900 focus:outline-none"
        />
        <button type="button" onClick={() => void load(query, true)} disabled={loading} className="rounded border border-gray-300 px-3 py-1.5 text-xs font-semibold text-gray-700 hover:bg-gray-50 disabled:opacity-50">
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>
      {error && <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      {listing?.error && (
        <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          The catalog could not be fetched, so this is the copy shipped with Compass {listing.core_version}. ({listing.error})
        </div>
      )}
      {listing && listing.entries.length === 0 && <div className="text-sm text-gray-500">{query ? "No captain matches." : "The catalog is empty."}</div>}
      <ul className="divide-y divide-gray-200 rounded border border-gray-200">
        {(listing?.entries || []).map((entry) => {
          const status = STATUS[entry.status];
          const isOpen = open === entry.name;
          return (
            <li key={entry.name} className="px-4 py-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-semibold text-gray-900">{entry.label}</span>
                    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${TIER_BADGE[entry.tier] || TIER_BADGE.open}`}>{entry.tier}</span>
                    <span className="text-[11px] text-gray-400">
                      v{entry.version}
                      {entry.installed_version && entry.installed_version !== entry.version ? ` (installed v${entry.installed_version})` : ""}
                    </span>
                    <span className={`inline-flex items-center rounded px-2 py-0.5 text-[11px] font-semibold ${status.cls}`}>{status.label}</span>
                  </div>
                  <p className="mt-1 text-sm text-gray-600">{entry.summary}</p>
                  <p className="mt-1 text-xs text-gray-400">
                    {entry.author ? `by ${entry.author}` : ""}
                    {entry.source && (
                      <>
                        {entry.author ? " · " : ""}
                        <a href={entry.source} target="_blank" rel="noopener noreferrer" className="underline hover:text-gray-600">source</a>
                      </>
                    )}
                    {entry.status === "incompatible" ? ` · needs Compass ${entry.requires_core}` : ""}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {entry.homepage && (
                    <a href={entry.homepage} target="_blank" rel="noopener noreferrer" className="rounded border border-gray-300 px-3 py-1 text-xs font-semibold text-gray-700 hover:bg-gray-50">
                      {entry.status === "vendor" ? "Get it" : "Details"}
                    </a>
                  )}
                  {(entry.status === "install" || entry.status === "update") && (
                    <button type="button" onClick={() => setOpen(isOpen ? null : entry.name)} className="rounded bg-gray-900 px-3 py-1 text-xs font-semibold text-white hover:opacity-90">
                      {isOpen ? "Close" : entry.status === "update" ? "Update" : "Install"}
                    </button>
                  )}
                </div>
              </div>
              {isOpen && <InstallSteps entry={entry} />}
            </li>
          );
        })}
      </ul>
      {listing?.source && !listing.error && <div className="text-[11px] text-gray-400">Catalog: {listing.source}</div>}
    </div>
  );
}
