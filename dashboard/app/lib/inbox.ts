"use client";
// The inbox as the dashboard sees it: one poll of GET /api/inbox shared by every
// subscriber (the sidebar's counts, the menu button's total, a tab's "Needs you"
// view, the whole feed), refreshed on the window events the tabs already fire and
// on `beamreach:inbox-updated`, which every inline action fires. docs/inbox.md.
import { useEffect, useState } from "react";

export type InboxAction = {
  key: string;
  label: string;
  method?: string;
  path?: string;
  body?: Record<string, unknown>;
  scope?: string;
  confirm?: string;
  href?: string;
  snooze?: boolean;
};

export type InboxItem = {
  id: string;
  kind: string;
  verb: "approve" | "fix" | "merge" | "later" | string;
  severity: "urgent" | "normal" | "info" | string;
  captain: string;
  tab: string;
  title: string;
  summary: string;
  since: number;
  subject: Record<string, unknown>;
  actions: InboxAction[];
  detail: Record<string, unknown>;
  snooze?: Record<string, unknown> | null;
};

export type InboxCounts = {
  total: number;
  urgent: number;
  by_tab: Record<string, number>;
  by_verb: Record<string, number>;
};

export type InboxFeedData = {
  generated_at: number;
  counts: InboxCounts;
  items: InboxItem[];
};

export const INBOX_UPDATED_EVENT = "beamreach:inbox-updated";
const REFRESH_EVENTS = [INBOX_UPDATED_EVENT, "beamreach:queue-updated", "beamreach:target-updated"];
const POLL_MS = 10_000;

type Listener = (feed: InboxFeedData | null, error: string | null) => void;

let current: InboxFeedData | null = null;
let currentError: string | null = null;
const listeners = new Set<Listener>();
let timer: number | null = null;
let inflight: Promise<void> | null = null;

const emit = () => listeners.forEach((l) => l(current, currentError));

export async function refreshInbox(fresh = false): Promise<void> {
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      const r = await fetch(`/api/inbox?include_snoozed=true${fresh ? "&fresh=true" : ""}`, { cache: "no-store" });
      if (!r.ok) throw new Error(`inbox: status ${r.status}`);
      current = (await r.json()) as InboxFeedData;
      currentError = null;
    } catch (error: any) {
      currentError = error?.message || "The inbox could not be read";
    } finally {
      inflight = null;
      emit();
    }
  })();
  return inflight;
}

const onEvent = () => void refreshInbox(true);

function start() {
  if (timer !== null || typeof window === "undefined") return;
  void refreshInbox();
  timer = window.setInterval(() => void refreshInbox(), POLL_MS);
  REFRESH_EVENTS.forEach((e) => window.addEventListener(e, onEvent));
}

function stop() {
  if (timer === null || typeof window === "undefined") return;
  window.clearInterval(timer);
  timer = null;
  REFRESH_EVENTS.forEach((e) => window.removeEventListener(e, onEvent));
}

export function subscribeInbox(listener: Listener): () => void {
  listeners.add(listener);
  listener(current, currentError);
  start();
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) stop();
  };
}

export function useInbox(): { feed: InboxFeedData | null; error: string | null; refresh: () => Promise<void> } {
  const [feed, setFeed] = useState<InboxFeedData | null>(current);
  const [error, setError] = useState<string | null>(currentError);
  useEffect(
    () =>
      subscribeInbox((f, e) => {
        setFeed(f);
        setError(e);
      }),
    [],
  );
  return { feed, error, refresh: () => refreshInbox(true) };
}

export const notifyInboxUpdated = () => {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(INBOX_UPDATED_EVENT));
};

/** The items a tab's "Needs you" view shows: the feed filtered to it, snoozed ones left out. */
export const itemsForTab = (feed: InboxFeedData | null, tab?: string | null): InboxItem[] =>
  (feed?.items || []).filter((i) => !i.snooze && (!tab || i.tab === tab));

export const countForTab = (feed: InboxFeedData | null, tab: string): number => feed?.counts?.by_tab?.[tab] || 0;

export const primaryAction = (item: InboxItem): InboxAction | undefined => item.actions.find((a) => a.method) || item.actions[0];

/** One line for the outcome row: what the route answered, in a few words. */
export const outcomeText = (action: InboxAction, result: any): string => {
  const verb = action.label.toLowerCase();
  if (!result || typeof result !== "object") return `${verb}: done`;
  const link = result.pr_url || result.grant_pr || result.url;
  if (typeof link === "string" && link) return `${verb}: ${link}`;
  if (typeof result.status === "string") return `${verb}: ${result.status}`;
  if (result.snoozed === true) return "hidden until it changes";
  if (typeof result.job_id === "string") return `${verb}: job ${result.job_id.slice(0, 8)} queued`;
  return `${verb}: done`;
};

/** Perform one of an item's actions: the route the tab already calls, with its body. */
export async function runInboxAction(action: InboxAction): Promise<any> {
  if (!action.path || !action.method) throw new Error("this action is a link");
  const r = await fetch(action.path, {
    method: action.method,
    headers: action.body ? { "Content-Type": "application/json" } : undefined,
    body: action.body ? JSON.stringify(action.body) : undefined,
  });
  let data: any = null;
  try {
    data = await r.json();
  } catch {
    data = null;
  }
  if (!r.ok) {
    const detail = data?.detail;
    throw new Error(typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : `status ${r.status}`);
  }
  notifyInboxUpdated();
  return data;
}
