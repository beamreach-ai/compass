// The demo's tour (docs/demo.md): seven steps whose completion comes from GET /api/demo/tour.
// The client-only steps are reported here, fire and forget: outside demo mode the route is a
// 404 and nothing happens.

import { incidentHref } from "../components/honeypotFormat";

export type TourStep = {
  id: string;
  kind: "client" | "api";
  title: string;
  why: string;
  done_when: string;
  done: boolean;
  skipped: boolean;
  detail: Record<string, any>;
};

export type Tour = { steps: TourStep[]; completed: number; total: number; current: string | null; finished: boolean };

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

/** Where a step takes the visitor: the API says what (a search term, a decoy, an incident), the dashboard where. */
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
      return null;
  }
};

export const TOUR_COLLAPSED_KEY = "compass:tour-collapsed";
