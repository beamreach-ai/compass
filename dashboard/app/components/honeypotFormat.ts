// Shared formatting helpers for the Honeypots tab, so every timestamp and id on
// the page reads the same way.

import { createContext, useContext } from "react";

// Read-only mode (demo stand): the API answers writes with 403, so every write
// control is disabled, with this title explaining why. HoneypotsTab provides
// the value from /api/settings/bootstrap.
export const READ_ONLY_TITLE = "Demo is read-only";
export const ReadOnlyContext = createContext(false);
export const useReadOnly = () => useContext(ReadOnlyContext);
/** Props for a write control: `{disabled, title}` in read-only mode, nothing otherwise. */
export const writeProps = (readOnly: boolean, disabled = false) =>
  readOnly ? { disabled: true, title: READ_ONLY_TITLE } : { disabled };

const toMs = (v: string | number | null | undefined): number => {
  if (v === null || v === undefined || v === "") return NaN;
  return typeof v === "number" ? v : Date.parse(v);
};

/** "just now", "40 s ago", "4 min ago", "2 h ago", "yesterday", else "2026-10-06". */
export function relativeTime(v: string | number | null | undefined, now: number = Date.now()): string {
  const ms = toMs(v);
  if (!Number.isFinite(ms)) return typeof v === "string" ? v : "";
  const s = Math.floor((now - ms) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 172800) return "yesterday";
  return new Date(ms).toISOString().slice(0, 10);
}

/** "2026-10-06 22:02:04 UTC". */
export function exactUtc(v: string | number | null | undefined): string {
  const ms = toMs(v);
  if (!Number.isFinite(ms)) return typeof v === "string" ? v : "";
  return `${new Date(ms).toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

/** Seconds between two timestamps as "47 s" / "1.5 s" / "2m 3s"; "" when either is missing. */
export function secondsBetween(from?: string | null, to?: string | null): string {
  const a = toMs(from);
  const b = toMs(to);
  if (!Number.isFinite(a) || !Number.isFinite(b)) return "";
  const s = (b - a) / 1000;
  if (s < 10) return `${s.toFixed(1)} s`;
  if (s < 120) return `${Math.round(s)} s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

/** Last `n` characters of an id/ARN tail ("…" prefix when shortened). */
export function tail(value: string, n = 12): string {
  const last = value.split("/").pop() || value;
  return last.length > n ? `…${last.slice(-n)}` : last;
}

// Buttons. global.css paints every <button> solid blue, so each variant states
// its own background, text colour and padding.
const BTN_BASE = "inline-flex items-center justify-center rounded-md text-sm font-medium transition disabled:opacity-50 disabled:cursor-not-allowed disabled:bg-gray-100 disabled:text-gray-400";
export const BTN_OUTLINE = `${BTN_BASE} px-3 py-1.5 border border-solid border-gray-300 bg-white text-gray-700 hover:bg-gray-50`;
export const BTN_OUTLINE_SM = "inline-flex items-center justify-center rounded-md px-2.5 py-1 text-xs font-medium border border-solid border-gray-300 bg-white text-gray-700 hover:bg-gray-50 transition disabled:opacity-50 disabled:cursor-not-allowed disabled:bg-gray-100";
export const BTN_GHOST = `${BTN_BASE} px-2 py-1.5 border border-solid border-transparent bg-transparent text-gray-600 hover:bg-gray-100 disabled:bg-transparent`;
export const BTN_QUIET_DANGER_SM = "inline-flex items-center justify-center rounded-md px-2.5 py-1 text-xs font-medium border border-solid border-red-200 bg-white text-red-700 hover:bg-red-50 transition disabled:opacity-50 disabled:cursor-not-allowed disabled:bg-gray-100";
export const LINK_BTN = "bg-transparent hover:bg-transparent p-0 text-blue-600 hover:underline font-medium";
