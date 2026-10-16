"use client";
// The progress of a running map scan: phase, a bar, and the details (region, resources
// found, time elapsed, about how long is left). Shown in the banner above the tabs and in
// the Maps tab's scan panel; mappingProgress.ts does the arithmetic.
import React, { useEffect, useState } from "react";
import { mappingProgress, progressDetails } from "./mappingProgress";
import type { ScanJob } from "./mappingProgress";

export default function MappingProgressBar({ job, previousMs = null }: { job: ScanJob; previousMs?: number | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(tick);
  }, []);
  const progress = mappingProgress(job, now, previousMs);
  return (
    <div className="mt-3 space-y-1.5">
      <div className="flex items-baseline justify-between gap-3 text-xs">
        <span className="font-semibold">{progress.phase}</span>
        <span className="tabular-nums">{progress.pct}%</span>
      </div>
      <div
        className="h-2 w-full overflow-hidden rounded-full bg-sky-100"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress.pct}
        aria-label="Mapping progress"
      >
        <div className="h-full rounded-full bg-sky-500 transition-all duration-700 ease-out" style={{ width: `${progress.pct}%` }} />
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs opacity-80">
        {progressDetails(progress).map((part) => (
          <span key={part}>{part}</span>
        ))}
      </div>
    </div>
  );
}
