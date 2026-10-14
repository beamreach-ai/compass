// The Pull requests tab is split into three views, selected by ?view=…; the
// Sidebar lists them as sub-items under "Pull requests" and PRsTab renders one.

export type PRView = "open" | "needs_you" | "history";

export const PR_VIEWS: { key: PRView; label: string }[] = [
  { key: "open", label: "Open" },
  { key: "needs_you", label: "Needs you" },
  { key: "history", label: "History" },
];

export const asPRView = (v: string | null | undefined): PRView =>
  PR_VIEWS.some((x) => x.key === v) ? (v as PRView) : "open";

export const prViewHref = (v: PRView) => (v === "open" ? "/workspace?tab=prs" : `/workspace?tab=prs&view=${v}`);
