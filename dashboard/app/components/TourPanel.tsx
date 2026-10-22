"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { useShell } from "../lib/shell";
import { completeTourStep, TOUR_COLLAPSED_KEY, tourStepHref, type Tour } from "../lib/tour";

// The tour: a panel bottom-right, collapsible to a pill ("Tour · 3 of 7"), opened by itself
// on a visitor's first visit, resumable from the pill. Steps are facts about the world,
// read from the API every 10 s while the panel is open; a client-only step posts itself.
export default function TourPanel() {
  const { settingsBootstrap } = useShell();
  const demo = settingsBootstrap?.app?.mode === "demo";
  const [tour, setTour] = useState<Tour | null>(null);
  const [open, setOpen] = useState<boolean | null>(null); // null: not decided yet (first load)

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/demo/tour");
      if (!res.ok) return;
      const next = (await res.json()) as Tour;
      setTour(next);
      setOpen((current) => {
        if (current !== null) return current;
        let collapsed = false;
        try {
          collapsed = window.sessionStorage.getItem(TOUR_COLLAPSED_KEY) === "1";
        } catch {
          collapsed = false;
        }
        // first visit: nothing done, nothing skipped, not collapsed this session
        return !collapsed && next.completed === 0 && !next.steps.some((s) => s.skipped);
      });
    } catch {
      // the tour is a convenience; the page works without it
    }
  }, []);

  useEffect(() => {
    if (!demo) return;
    void load();
  }, [demo, load]);
  useEffect(() => {
    if (!demo || !open) return;
    const id = setInterval(() => void load(), 10_000);
    return () => clearInterval(id);
  }, [demo, open, load]);

  const toggle = (next: boolean) => {
    setOpen(next);
    try {
      window.sessionStorage.setItem(TOUR_COLLAPSED_KEY, next ? "0" : "1");
    } catch {
      // nothing worse than the panel opening again on reload
    }
  };

  const skip = async (id: string) => {
    try {
      const res = await fetch(`/api/demo/tour/${id}/skip`, { method: "POST" });
      if (res.ok) setTour((await res.json()) as Tour);
    } catch {
      // the next poll shows the state
    }
  };

  if (!demo || !tour) return null;

  if (!open) {
    return (
      <button
        type="button"
        data-tour-pill
        onClick={() => toggle(true)}
        className="fixed bottom-4 right-4 z-30 rounded-full border border-amber-300 bg-amber-50 px-4 py-2 text-sm font-semibold text-amber-900 shadow-lg hover:bg-amber-100"
      >
        Tour · {tour.completed} of {tour.total}
      </button>
    );
  }

  const current = tour.steps.find((s) => s.id === tour.current) ?? null;
  return (
    <aside
      data-tour-panel
      aria-label="Tour"
      className="fixed bottom-4 right-4 z-30 flex max-h-[70vh] w-[min(92vw,380px)] flex-col rounded-lg border border-amber-300 bg-white shadow-xl"
    >
      <div className="flex items-center justify-between gap-3 border-b border-amber-200 bg-amber-50 px-4 py-2">
        <div className="text-sm font-semibold text-amber-900">
          Tour · {tour.completed} of {tour.total}
          {tour.finished && <span className="ml-2 font-normal text-amber-800">done</span>}
        </div>
        <button type="button" onClick={() => toggle(false)} aria-label="Collapse the tour" className="rounded px-1 text-base leading-none text-amber-700 hover:bg-amber-100 hover:text-amber-900">
          –
        </button>
      </div>
      <ol className="min-h-0 flex-1 space-y-1 overflow-y-auto px-3 py-2">
        {tour.steps.map((step, index) => {
          const active = step.id === tour.current;
          const href = tourStepHref(step);
          return (
            <li key={step.id} data-tour-step={step.id} data-tour-done={step.done ? "1" : "0"} className={`rounded-md px-2 py-2 ${active ? "bg-amber-50" : ""}`}>
              <div className="flex items-start gap-2">
                <span
                  className={`mt-0.5 inline-flex h-5 w-5 flex-none items-center justify-center rounded-full text-[11px] font-bold ${
                    step.done ? "bg-green-600 text-white" : step.skipped ? "bg-gray-200 text-gray-600" : active ? "bg-amber-500 text-white" : "bg-gray-100 text-gray-600"
                  }`}
                  aria-hidden="true"
                >
                  {step.done ? "✓" : index + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <div className={`text-sm font-semibold ${step.done ? "text-gray-500 line-through" : "text-gray-900"}`}>{step.title}</div>
                  {(active || (!step.done && !step.skipped)) && <p className="mt-0.5 text-xs text-gray-600">{step.why}</p>}
                  {active && step.id !== "run_it" && (
                    <div className="mt-1.5 flex flex-wrap items-center gap-3 text-xs">
                      {href && (
                        <Link href={href} className="font-semibold text-blue-700 hover:underline">
                          Open →
                        </Link>
                      )}
                      <span className="text-gray-500">{step.done_when}</span>
                      <button type="button" onClick={() => void skip(step.id)} className="bg-transparent p-0 text-gray-500 hover:bg-transparent hover:text-gray-800 hover:underline">
                        Skip
                      </button>
                    </div>
                  )}
                  {active && step.id === "response" && step.detail?.links?.slack && (
                    <a
                      href={String(step.detail.links.slack)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="mt-2 block text-xs font-semibold text-blue-700 hover:underline"
                    >
                      The alert lands in Slack too: join the channel →
                    </a>
                  )}
                  {active && step.id === "run_it" && (
                    <div className="mt-2 space-y-1.5 rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-xs">
                      <a
                        href={String(step.detail?.links?.install ?? "https://beamreach.ai/compass/install.html")}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={() => completeTourStep("run_it")}
                        className="block font-semibold text-blue-700 hover:underline"
                      >
                        Install Compass: one compose file →
                      </a>
                      <a
                        href={String(step.detail?.links?.repository ?? "https://github.com/beamreach-ai/compass")}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={() => completeTourStep("run_it")}
                        className="block text-blue-700 hover:underline"
                      >
                        The repository on GitHub →
                      </a>
                      <p className="text-gray-600">`compass connect` uses your profile once; the services never see it.</p>
                      <button type="button" onClick={() => void skip(step.id)} className="bg-transparent p-0 text-gray-500 hover:bg-transparent hover:text-gray-800 hover:underline">
                        Skip
                      </button>
                    </div>
                  )}
                  {step.skipped && !step.done && <div className="text-xs text-gray-400">skipped</div>}
                </div>
              </div>
            </li>
          );
        })}
      </ol>
      {tour.finished && !current && (
        <div className="border-t border-gray-200 px-4 py-2 text-xs text-gray-600">That is the demo. The install page has the rest.</div>
      )}
    </aside>
  );
}
