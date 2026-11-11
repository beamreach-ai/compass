"use client";

import { useEffect, useState } from "react";

// The demo's funnel, for its operator (Settings, demo admins only): visitors of the last
// N days by where they came from and which story, how far they got, who opened a pull
// request, who clicked the pilot link. GET /api/demo/funnel; 403 for anyone else, and then
// nothing renders.
type Bucket = { visitors: number; steps: Record<string, number>; pr_opened: number; pilot_click: number; install_click: number; repository_click: number; finished: number };
type Funnel = { since: number; until: number; steps: string[]; total: Bucket; by_source: (Bucket & { src: string; story: string })[]; by_story: (Bucket & { story: string })[] };

const DAYS = [7, 30, 90];

export default function DemoFunnel() {
  const [days, setDays] = useState(30);
  const [funnel, setFunnel] = useState<Funnel | null>(null);
  const [forbidden, setForbidden] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/demo/funnel?days=${days}`);
        if (res.status === 403 || res.status === 404) {
          if (!cancelled) setForbidden(true);
          return;
        }
        if (!res.ok) return;
        const next = (await res.json()) as Funnel;
        if (!cancelled) setFunnel(next);
      } catch {
        // the card is a convenience
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [days]);

  if (forbidden || !funnel) return null;
  const pct = (n: number, of: number) => (of > 0 ? `${Math.round((n / of) * 100)}%` : "–");
  const keySteps = funnel.steps.filter((s) => ["unmanaged", "import_pr", "cost_fix", "decoys", "trip", "run_it"].includes(s));

  return (
    <section data-demo-funnel className="rounded-lg border border-gray-200 bg-white p-5">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">Demo funnel</h2>
          <p className="text-sm text-gray-600">
            Visitors by where they came from and which story, and how far they got. Counted from the tours and the pull requests; nobody is named.
          </p>
        </div>
        <div className="flex items-center gap-1 text-xs">
          {DAYS.map((d) => (
            <button
              key={d}
              type="button"
              onClick={() => setDays(d)}
              className={`rounded px-2 py-1 ${d === days ? "bg-gray-900 text-white" : "bg-gray-100 text-gray-700 hover:bg-gray-200"}`}
            >
              {d} days
            </button>
          ))}
        </div>
      </div>
      <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          ["Visitors", funnel.total.visitors, null],
          ["Opened a pull request", funnel.total.pr_opened, pct(funnel.total.pr_opened, funnel.total.visitors)],
          ["Clicked the pilot link", funnel.total.pilot_click, pct(funnel.total.pilot_click, funnel.total.visitors)],
          ["Finished the tour", funnel.total.finished, pct(funnel.total.finished, funnel.total.visitors)],
        ].map(([label, n, share]) => (
          <div key={String(label)} className="rounded-md border border-gray-100 bg-gray-50 px-3 py-2">
            <div className="text-xs text-gray-500">{label}</div>
            <div className="text-xl font-semibold text-gray-900">
              {n}
              {share && <span className="ml-2 text-sm font-normal text-gray-500">{share}</span>}
            </div>
          </div>
        ))}
      </div>
      {funnel.by_source.length === 0 ? (
        <p className="text-sm text-gray-500">No visitors in the last {days} days.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-gray-200 text-xs uppercase tracking-wide text-gray-500">
                <th className="py-2 pr-3">Source</th>
                <th className="py-2 pr-3">Story</th>
                <th className="py-2 pr-3 text-right">Visitors</th>
                {keySteps.map((s) => (
                  <th key={s} className="py-2 pr-3 text-right">
                    {s.replace("_", " ")}
                  </th>
                ))}
                <th className="py-2 pr-3 text-right">PR</th>
                <th className="py-2 pr-3 text-right">Pilot</th>
              </tr>
            </thead>
            <tbody>
              {funnel.by_source.map((row) => (
                <tr key={`${row.src}/${row.story}`} className="border-b border-gray-100">
                  <td className="py-2 pr-3 font-medium text-gray-900">{row.src}</td>
                  <td className="py-2 pr-3 text-gray-700">{row.story}</td>
                  <td className="py-2 pr-3 text-right">{row.visitors}</td>
                  {keySteps.map((s) => (
                    <td key={s} className="py-2 pr-3 text-right text-gray-700">
                      {row.steps[s] ?? 0}
                    </td>
                  ))}
                  <td className="py-2 pr-3 text-right font-medium">{row.pr_opened}</td>
                  <td className="py-2 pr-3 text-right font-medium text-amber-800">{row.pilot_click}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
