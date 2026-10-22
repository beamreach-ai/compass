"use client";
// Updates of Compass itself (docs/open_updates_spec.md §3.2): the banner above the
// tabs when a release is out, and the Compass card on Settings with the running and
// pinned versions, the policy, Update (with the updater service) or the two commands
// (without it), the release notes of every version in between, and Roll back for seven
// days after an update.
import axios from "axios";
import Link from "next/link";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { DASHBOARD_VERSION } from "../lib/edition";
import { useShell } from "../lib/shell";
import type { ReleaseNote, UpdateState } from "../lib/shell";
import { notifyInboxUpdated } from "../lib/inbox";
import { dismissVersion, dismissedVersion, httpsLink, noteDate, phaseText, POLICY_LABELS, relativeTime, versionsDiffer, WHATS_NEW_ID } from "./updateViews";
import type { UpdatesListing } from "./updateViews";

const errorText = (e: any, fallback: string): string => {
  const detail = e?.response?.data?.detail;
  return (typeof detail === "string" && detail) || e?.message || fallback;
};

const sleep = (ms: number) => new Promise((r) => window.setTimeout(r, ms));

// After a request: follow update.result.json; when the API goes away, follow /health
// and reload once a different version answers.
async function followUpdate(from: string, onListing: (l: UpdatesListing) => void): Promise<"reloaded" | "failed" | "timeout"> {
  const deadline = Date.now() + 70 * 60 * 1000; // the request may wait up to an hour for jobs
  while (Date.now() < deadline) {
    await sleep(3000);
    try {
      const res = await axios.get("/api/updates", { timeout: 4000 });
      const listing = res.data as UpdatesListing;
      onListing(listing);
      if (listing.update.running !== from) {
        window.location.reload();
        return "reloaded";
      }
      if (listing.result?.status === "failed" && !listing.request && !listing.pending) return "failed";
    } catch {
      // the API is being recreated; keep polling until it answers
    }
  }
  return "timeout";
}

function Commands({ lines }: { lines: string[] }) {
  const [copied, setCopied] = useState(false);
  const text = lines.join("\n");
  return (
    <div className="flex items-start gap-2">
      <pre className="flex-1 overflow-x-auto whitespace-pre-wrap break-all rounded border border-gray-200 bg-white p-2 font-mono text-[11px]">{text}</pre>
      <button
        type="button"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(text);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          } catch {
            setCopied(false);
          }
        }}
        className="shrink-0 rounded border border-gray-300 bg-white px-2 py-1 text-[11px] font-semibold text-gray-700 hover:bg-gray-50"
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

// The notes feed's releases, as plain text: React escapes every string, nothing is HTML.
function WhatsNew({ notes }: { notes: ReleaseNote[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const [arrived, setArrived] = useState(false);
  useEffect(() => {
    // the banner's "What's new" links here
    const land = () => {
      if (window.location.hash !== `#${WHATS_NEW_ID}`) return;
      setArrived(true);
      ref.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    };
    land();
    window.addEventListener("hashchange", land);
    return () => window.removeEventListener("hashchange", land);
  }, []);
  return (
    <div id={WHATS_NEW_ID} ref={ref} className="space-y-2 border-t border-gray-200 pt-3">
      <div className="text-xs font-semibold text-gray-700">What&apos;s new</div>
      {notes.map((note, index) => {
        const link = httpsLink(note.link);
        const date = noteDate(note.published_at);
        return (
          <details key={note.version} open={notes.length === 1 || (arrived && index === 0)} className="rounded border border-gray-200 bg-white px-3 py-2">
            <summary className="cursor-pointer text-sm text-gray-900">
              <span className="font-semibold">{note.version}</span>
              {note.headline ? ` — ${note.headline}` : ""}
              {date && <span className="ml-2 text-xs text-gray-500">{date}</span>}
            </summary>
            <div className="mt-2 space-y-3 text-xs text-gray-700">
              {note.summary && <p>{note.summary}</p>}
              {note.features.length > 0 && (
                <ul className="space-y-2">
                  {note.features.map((feature) => (
                    <li key={feature.title || feature.body}>
                      {feature.title && <div className="font-semibold text-gray-900">{feature.title}</div>}
                      {feature.body && <p>{feature.body}</p>}
                    </li>
                  ))}
                </ul>
              )}
              {note.after.length > 0 && (
                <div>
                  <div className="font-semibold text-amber-800">After the update</div>
                  <ul className="list-disc space-y-1 pl-5">
                    {note.after.map((line) => (
                      <li key={line}>{line}</li>
                    ))}
                  </ul>
                </div>
              )}
              {link && (
                <a href={link.href} target="_blank" rel="noopener noreferrer" className="inline-block text-blue-600 underline">
                  Read on {link.host}
                </a>
              )}
            </div>
          </details>
        );
      })}
    </div>
  );
}

// ── the banner ──────────────────────────────────────────────────────────────

export function UpdateBanner({ update, settingsHref }: { update: UpdateState | null | undefined; settingsHref: string }) {
  const [dismissed, setDismissed] = useState<string | null>(null);
  useEffect(() => setDismissed(dismissedVersion()), []);
  if (!update) return null;
  const mismatch = versionsDiffer(DASHBOARD_VERSION, update.running);
  const showRelease = update.available && update.latest && dismissed !== update.latest;
  if (!showRelease && !mismatch && !update.pinned_differs) return null;
  return (
    <div className="mb-6 space-y-2">
      {showRelease && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-sky-200 bg-sky-50 px-4 py-3 text-sm text-sky-900">
          <span>
            Compass {update.latest} is available; you run {update.running}.
            {update.release?.summary ? ` ${update.release.summary}` : ""}
          </span>
          <span className="flex shrink-0 items-center gap-3">
            {update.release_notes?.length ? (
              <Link href={`${settingsHref}#${WHATS_NEW_ID}`} className="font-semibold underline underline-offset-2">
                What&apos;s new
              </Link>
            ) : update.release?.notes_url && (
              <a href={update.release.notes_url} target="_blank" rel="noopener noreferrer" className="font-semibold underline underline-offset-2">
                What changed
              </a>
            )}
            <Link href={settingsHref} className="rounded bg-gray-900 px-3 py-1 text-xs font-semibold text-white hover:opacity-90">
              Update
            </Link>
            <button
              type="button"
              aria-label="Dismiss until the next release"
              onClick={() => {
                dismissVersion(update.latest || "");
                void fetch(`/api/inbox/update.available:${encodeURIComponent(update.latest || "")}/snooze`, { method: "POST" }).then(() => notifyInboxUpdated()).catch(() => undefined);
                setDismissed(update.latest || null);
              }}
              className="text-sky-700 hover:text-sky-900"
            >
              ×
            </button>
          </span>
        </div>
      )}
      {update.pinned_differs && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          The compose file asks for {update.pinned} but {update.running} is running. Run <code className="font-mono">docker compose up -d</code> next to your <code>docker-compose.yml</code>.
        </div>
      )}
      {mismatch && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          The dashboard is {DASHBOARD_VERSION} and the API is {update.running}. Reload the page; if this stays, run <code className="font-mono">docker compose pull && docker compose up -d</code>.
        </div>
      )}
    </div>
  );
}

// ── the Settings card ───────────────────────────────────────────────────────

export default function CompassSection({ readOnly }: { readOnly: boolean }) {
  const { refreshSettingsBootstrap } = useShell();
  const [listing, setListing] = useState<UpdatesListing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<{ code: string; detail: string; captains?: Record<string, string>; commands?: string[] } | null>(null);
  const following = useRef(false);

  const load = useCallback(async (refresh = false) => {
    try {
      const res = await axios.get("/api/updates", { params: { refresh: refresh || undefined } });
      setListing(res.data as UpdatesListing);
      setError(null);
    } catch (e: any) {
      setError(errorText(e, "Failed to load the update status"));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const follow = useCallback(
    async (from: string) => {
      if (following.current) return;
      following.current = true;
      const outcome = await followUpdate(from, setListing);
      following.current = false;
      if (outcome === "timeout") setError("The update did not finish within an hour. Check `docker compose logs updater`.");
      await load();
    },
    [load],
  );

  // a request made elsewhere (the CLI, the policy, another tab) is followed too
  useEffect(() => {
    if (listing && (listing.request || listing.pending || ["pulling", "waiting", "applying"].includes(listing.result?.status || ""))) {
      void follow(listing.update.running);
    }
  }, [listing, follow]);

  const post = async (path: string, body: Record<string, unknown>, label: string) => {
    if (!listing) return;
    setBusy(label);
    setError(null);
    setRefusal(null);
    try {
      await axios.post(path, body);
      await load();
    } catch (e: any) {
      const data = e?.response?.data;
      if (data?.code) setRefusal({ code: data.code, detail: data.detail, captains: data.captains, commands: data.commands });
      else setError(errorText(e, `${label} failed`));
    } finally {
      setBusy(null);
    }
  };

  const setPolicy = async (auto: string) => {
    setBusy("policy");
    try {
      await axios.put("/api/updates/policy", { auto });
      await load(true);
      await refreshSettingsBootstrap();
    } catch (e: any) {
      setError(errorText(e, "Failed to save the update policy"));
    } finally {
      setBusy(null);
    }
  };

  const u = listing?.update;
  const phase = phaseText(listing);
  const last = listing?.result;
  const target = u?.available ? u.latest : null;

  return (
    <section className="bg-white border rounded-lg shadow-sm p-6 space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-sm text-gray-600">Nothing changes version without a click unless you choose a policy below.</p>
        <button type="button" onClick={() => void load(true)} disabled={!listing || busy !== null} className="rounded border border-gray-300 px-3 py-1 text-xs font-semibold text-gray-700 hover:bg-gray-50 disabled:opacity-50">
          Check now
        </button>
      </div>
      {error && <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      {!listing && !error && <div className="text-sm text-gray-500">Loading…</div>}
      {u && listing && (
        <>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
            <div>
              <dt className="text-xs text-gray-500">Running</dt>
              <dd className="font-semibold text-gray-900">{u.running}</dd>
            </div>
            <div>
              <dt className="text-xs text-gray-500">Compose file asks for</dt>
              <dd className={u.pinned_differs ? "font-semibold text-amber-700" : "text-gray-900"}>{u.pinned || "not passed in"}</dd>
            </div>
            <div>
              <dt className="text-xs text-gray-500">Channel</dt>
              <dd className="text-gray-900">{u.channel}</dd>
            </div>
            <div>
              <dt className="text-xs text-gray-500">Last check</dt>
              <dd className="text-gray-900">{u.apply === "off" ? "turned off" : relativeTime(u.checked_at)}</dd>
            </div>
          </dl>
          {u.error && u.apply !== "off" && <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">The release feed could not be read: {u.error}</div>}

          {phase && <div className="rounded border border-sky-200 bg-sky-50 px-3 py-2 text-sm text-sky-900">{phase}</div>}
          {last?.status === "failed" && !phase && (
            <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              The {last.mode === "auto" ? "automatic " : ""}update to {last.to} failed; {last.from} is still running. {last.error}
              {last.mode === "auto" ? " It is not retried for this version." : ""}
            </div>
          )}
          {last?.status === "updated" && !phase && last.to === u.running && (
            <div className="text-xs text-green-700">Updated from {last.from} to {last.to} {relativeTime(last.at)}.</div>
          )}

          {target && u.release ? (
            <div className="space-y-3 rounded border border-gray-200 bg-gray-50 p-4 text-sm">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="font-semibold text-gray-900">Compass {target}</span>
                {u.release.notes_url && (
                  <a href={u.release.notes_url} target="_blank" rel="noopener noreferrer" className="text-xs text-blue-600 underline">
                    What changed
                  </a>
                )}
              </div>
              {u.release.summary && <p className="text-gray-700">{u.release.summary}</p>}
              {u.release.notes.length > 0 && (
                <ul className="list-disc space-y-1 pl-5 text-xs text-gray-600">
                  {u.release.notes.map((note) => (
                    <li key={note}>{note}</li>
                  ))}
                </ul>
              )}
              {u.release_notes?.length ? (
                <WhatsNew notes={u.release_notes} />
              ) : (
                u.notes_error && <p className="text-xs text-gray-500">The release notes could not be read: {u.notes_error}</p>
              )}
              {!u.direct && (
                <p className="text-xs text-amber-700">
                  {target} upgrades from {u.release.min_from} or later. Update to {u.via || u.release.min_from} first.
                </p>
              )}
              {u.apply === "updater" && !readOnly ? (
                <button
                  type="button"
                  disabled={busy !== null || Boolean(phase)}
                  onClick={() => void post("/api/updates/apply", { target: u.direct ? target : u.via || target }, "Update")}
                  className="rounded bg-gray-900 px-3 py-1.5 text-xs font-semibold text-white hover:opacity-90 disabled:opacity-50"
                >
                  {busy === "Update" ? "Requesting…" : `Update to ${u.direct ? target : u.via || target}`}
                </button>
              ) : (
                <div className="space-y-2 text-xs text-gray-700">
                  <div>
                    Run this next to your <code>docker-compose.yml</code>:
                  </div>
                  <Commands lines={listing.commands} />
                  <div className="text-gray-500">
                    For one-click updates, start the updater: <code className="font-mono">docker compose --profile updater up -d</code>. It holds the Docker socket, which is root on that machine.
                  </div>
                </div>
              )}
            </div>
          ) : (
            u.apply !== "off" && !u.error && <div className="text-sm text-gray-600">This is the latest {u.channel} release.</div>
          )}

          {refusal && (
            <div className="space-y-2 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
              <div>{refusal.detail}</div>
              {refusal.captains && (
                <ul className="list-disc pl-5 text-xs">
                  {Object.entries(refusal.captains).map(([name, spec]) => (
                    <li key={name}>
                      {name} needs Compass {spec}
                    </li>
                  ))}
                </ul>
              )}
              {refusal.commands && <Commands lines={refusal.commands} />}
              {["captains_incompatible", "downgrade", "not_direct"].includes(refusal.code) && target && !readOnly && (
                <button
                  type="button"
                  onClick={() => void post("/api/updates/apply", { target, force: true }, "Update")}
                  className="rounded border border-amber-400 bg-white px-3 py-1 text-xs font-semibold text-amber-900 hover:bg-amber-100"
                >
                  Update anyway
                </button>
              )}
            </div>
          )}

          {listing.rollback && !phase && (
            <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-gray-600">
              <span>
                {listing.rollback.to} is still on this machine until {new Date(listing.rollback.until * 1000).toLocaleDateString()}.
              </span>
              {u.apply === "updater" && !readOnly && (
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={() => {
                    if (window.confirm(`Roll back to ${listing.rollback?.to}? Data written since the update stays.`)) void post("/api/updates/rollback", {}, "Roll back");
                  }}
                  className="rounded border border-gray-300 px-3 py-1 text-xs font-semibold text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                >
                  Roll back to {listing.rollback.to}
                </button>
              )}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-3 border-t border-gray-100 pt-3 text-sm">
            <label htmlFor="update-policy" className="text-gray-700">
              Updates
            </label>
            <select
              id="update-policy"
              value={listing.policy.auto}
              disabled={readOnly || busy !== null}
              onChange={(e) => void setPolicy(e.target.value)}
              className="rounded border border-gray-300 px-2 py-1 text-sm disabled:opacity-50"
            >
              {(Object.keys(POLICY_LABELS) as Array<keyof typeof POLICY_LABELS>).map((key) => (
                <option key={key} value={key}>
                  {POLICY_LABELS[key]}
                </option>
              ))}
            </select>
            <span className="text-xs text-gray-500">
              {listing.policy.window ? `Automatic updates run ${listing.policy.window} only. ` : ""}
              {(listing.policy.auto === "patch" || listing.policy.auto === "minor") && !listing.updater.alive ? "Automatic updates need the updater service running." : ""}
            </span>
          </div>
          <div className="text-xs text-gray-400">
            Updater: {listing.updater.alive ? `running${listing.updater.version ? ` (${listing.updater.version})` : ""}` : "not running"}
            {listing.jobs && listing.jobs.active_jobs > 0 ? ` · ${listing.jobs.active_jobs} jobs running; an update waits for them` : ""}
            {` · Dashboard ${DASHBOARD_VERSION}`}
          </div>
        </>
      )}
    </section>
  );
}
