import { NextRequest, NextResponse } from "next/server";

const ALL_TABS = ["audits", "finops", "compliance", "queue", "maps", "policies", "settings"] as const;
const TAB_FEATURE_MAP: Record<string, string> = {
  audits: "security",
  finops: "finops",
  compliance: "compliance",
  queue: "queue",
  maps: "maps",
  policies: "policies",
  settings: "settings",
};

const resolveEnabledTabs = () => {
  const disabled = new Set(
    (process.env.DISABLED_FEATURES || "")
      .split(",")
      .map((item) => item.trim().toLowerCase())
      .filter(Boolean),
  );
  const enabled = ALL_TABS.filter((tab) => !disabled.has(TAB_FEATURE_MAP[tab]));
  return enabled.length > 0 ? enabled : ["finops"];
};

const resolveDefaultTab = () => {
  const enabledTabs = resolveEnabledTabs();
  const value = (process.env.DEFAULT_TAB || "").trim().toLowerCase();
  return enabledTabs.includes(value as (typeof ALL_TABS)[number])
    ? value
    : enabledTabs[0] || "workspace";
};

export async function GET(req: NextRequest) {
  const hostHeader = req.headers.get("host");
  const url = new URL(req.url);
  const host = hostHeader || url.host;
  const protocol =
    host?.includes("localhost") || url.protocol === "http:" ? "http" : "https";

  const defaultTab = resolveDefaultTab();
  const path = defaultTab === "workspace" ? "/workspace" : `/workspace?tab=${defaultTab}`;
  const redirectTarget = host ? `${protocol}://${host}${path}` : path;
  const response = NextResponse.redirect(redirectTarget, 302);
  response.headers.set("Cache-Control", "no-store");
  return response;
}
