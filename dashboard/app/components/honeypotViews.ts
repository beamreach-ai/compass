// The Honeypots tab is split into four views, selected by ?view=…; the Sidebar
// lists them as sub-items under "Honeypots" and HoneypotsTab renders one.

export type HoneypotView = "overview" | "honeytokens" | "history" | "settings";

export const HONEYPOT_VIEWS: { key: HoneypotView; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "honeytokens", label: "Honeytokens" },
  { key: "history", label: "History" },
  { key: "settings", label: "Settings" },
];

export const asHoneypotView = (v: string | null | undefined): HoneypotView =>
  HONEYPOT_VIEWS.some((x) => x.key === v) ? (v as HoneypotView) : "overview";

export const honeypotViewHref = (v: HoneypotView) =>
  v === "overview" ? "/workspace?tab=honeypots" : `/workspace?tab=honeypots&view=${v}`;
