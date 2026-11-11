// The demo's tour (docs/demo.md): seven steps whose completion comes from GET /api/demo/tour.
// The client-only steps are reported here, fire and forget: outside demo mode the route is a
// 404 and nothing happens. A visitor arrives on a story (`?story=drift|honeytoken|cost`) from
// a source (`?src=`, `?gclid=`): the landing page keeps them for the session, the panel posts
// them once to /api/demo/tour/start, and every exit to the pilot page carries them on.

import { incidentHref } from "../components/honeypotFormat";

export type TourStep = {
  id: string;
  kind: "client" | "api";
  pr?: boolean;
  title: string;
  why: string;
  done_when: string;
  done: boolean;
  skipped: boolean;
  detail: Record<string, any>;
};

export type TourPilot = { url: string; note: string };

export type Tour = {
  steps: TourStep[];
  completed: number;
  total: number;
  current: string | null;
  finished: boolean;
  story?: string | null;
  source?: Record<string, string>;
  pilot?: TourPilot | null;
};

export type TourSource = { story?: string; src?: string; gclid?: string; referrer?: string };

export const TOUR_COLLAPSED_KEY = "compass:tour-collapsed";
export const TOUR_SOURCE_KEY = "compass:tour-source";
export const TOUR_STARTED_KEY = "compass:tour-started";
const SOURCE_PARAMS = ["story", "src", "gclid"] as const;

/** The landing's `?story=`, `?src=` and `?gclid=`, kept for the session so they survive the sign-in round trip.
 *  Called on every page; the first page that has any of them wins. */
export const captureTourSource = (): TourSource | null => {
  try {
    const params = new URLSearchParams(window.location.search);
    const found: TourSource = {};
    for (const key of SOURCE_PARAMS) {
      const value = params.get(key);
      if (value) found[key] = value.slice(0, 200);
    }
    if (document.referrer && !document.referrer.startsWith(window.location.origin)) found.referrer = document.referrer.slice(0, 200);
    if (!found.story && !found.src && !found.gclid) return readTourSource();
    if (window.sessionStorage.getItem(TOUR_SOURCE_KEY)) return readTourSource();
    window.sessionStorage.setItem(TOUR_SOURCE_KEY, JSON.stringify(found));
    return found;
  } catch {
    return null;
  }
};

export const readTourSource = (): TourSource | null => {
  try {
    const raw = window.sessionStorage.getItem(TOUR_SOURCE_KEY);
    return raw ? (JSON.parse(raw) as TourSource) : null;
  } catch {
    return null;
  }
};

/** Tell the API the visitor arrived, once per session: the story and the source go on their tour row. */
export const startTour = async (source: TourSource | null): Promise<Tour | null> => {
  try {
    const res = await fetch("/api/demo/tour/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(source ?? {}),
    });
    if (!res.ok) return null;
    try {
      window.sessionStorage.setItem(TOUR_STARTED_KEY, "1");
    } catch {
      // without storage the start posts again next load; the API writes it once anyway
    }
    return (await res.json()) as Tour;
  } catch {
    return null;
  }
};

export const tourStarted = (): boolean => {
  try {
    return window.sessionStorage.getItem(TOUR_STARTED_KEY) === "1";
  } catch {
    return false;
  }
};

export type TourEvent = "pilot_click" | "install_click" | "repository_click";

/** An exit the visitor took (not a step): counted in the funnel, fire and forget. */
export const recordTourEvent = (event: TourEvent, href?: string | null): void => {
  try {
    void fetch("/api/demo/tour/event", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event, href: href ?? null }),
      keepalive: true,
    }).catch(() => undefined);
  } catch {
    // the funnel is a convenience
  }
};

export const completeTourStep = (step: string, body?: { incident_id?: string | null }): void => {
  try {
    void fetch(`/api/demo/tour/${encodeURIComponent(step)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    }).catch(() => undefined);
  } catch {
    // nothing to do: the tour is a convenience
  }
};

/** Where a step takes the visitor: the API says what (a search term, a decoy, an incident, a path), the dashboard where. */
export const tourStepHref = (step: Pick<TourStep, "id" | "detail">): string | null => {
  const d = step.detail ?? {};
  const q = (params: Record<string, string | null | undefined>) => {
    const out = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) out.set(k, v);
    return `/workspace?${out.toString()}`;
  };
  switch (step.id) {
    case "map":
      return q({ tab: "maps", q: d.search ?? "payments" });
    case "unmanaged":
      return q({ tab: "iac" });
    case "import_pr":
      return q({ tab: "iac", q: d.search });
    case "decoys":
    case "trip":
      return q({ tab: "honeypots", view: "honeytokens", token: d.token });
    case "response":
      return d.incident_id ? incidentHref(String(d.incident_id)) : q({ tab: "honeypots", view: "history" });
    default:
      // a product's step says where it opens
      return typeof d.href === "string" && d.href.startsWith("/") ? d.href : null;
  }
};

/** The pilot page, with where this visitor came from: the funnel's exit carries its source. */
export const pilotHref = (tour: Pick<Tour, "pilot" | "story" | "source">): string | null => {
  const base = tour.pilot?.url;
  if (!base) return null;
  try {
    const url = new URL(base);
    url.searchParams.set("src", "demo");
    if (tour.story) url.searchParams.set("story", tour.story);
    const gclid = tour.source?.gclid ?? readTourSource()?.gclid;
    if (gclid) url.searchParams.set("gclid", gclid);
    const from = tour.source?.src ?? readTourSource()?.src;
    if (from) url.searchParams.set("from", from);
    return url.toString();
  } catch {
    return base;
  }
};
