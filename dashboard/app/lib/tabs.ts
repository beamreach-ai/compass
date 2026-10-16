// The dashboard tabs. A tab key is any slug: the installed captains declare the
// tabs (compass_core.captains), the API lists the enabled ones in
// /api/settings/bootstrap (`enabled_tabs`) with their labels and groups (`tabs`),
// and the shell renders the component registered for the key
// (app/lib/captains.ts for the open tabs, app/lib/extensions.ts for the product's).
// ALL_WORKSPACE_TABS is only the fallback before the bootstrap has answered.
export type WorkspaceTab = string;
export type DashboardTab = string;
export const ALL_WORKSPACE_TABS: WorkspaceTab[] = ["finops", "audits", "compliance", "queue", "maps", "iac", "prs", "policies", "settings", "honeypots"];

// Settings is one page in sections; the sidebar lists them under Settings, the way a
// captain's views are listed under its tab, and each link scrolls to its section
// (id `settings-<key>`). Updates is the open edition's (the product deploys its own way).
export const SETTINGS_VIEWS = (openEdition: boolean): string[] =>
  openEdition ? ["integrations", "captains", "updates"] : ["integrations", "captains"];
export const settingsSectionId = (view: string): string => `settings-${view}`;

// Legacy URLs: `?tab=radio` has always opened the audits tab.
export const TAB_ALIASES: Partial<Record<string, WorkspaceTab>> = { radio: "audits" };

export type TabGroup = "cost" | "security" | "platform";
export const TAB_GROUPS: TabGroup[] = ["cost", "security", "platform"];

// One entry of the bootstrap's `app.tabs`: what the sidebar needs to draw a tab.
export type TabDetail = {
  key: WorkspaceTab;
  label: string;
  group: TabGroup | string;
  captain?: string | null;
  views?: string[];
  feature?: string | null;
};

export type AppConfigSummary = {
  mode?: "production" | "demo" | null;
  read_only?: boolean;
  settings_enabled?: boolean;
  default_tab?: WorkspaceTab | null;
  enabled_tabs?: WorkspaceTab[] | null;
  tabs?: TabDetail[] | null;
  disabled_features?: string[] | null;
  production_url?: string | null;
};

const SLUG = /^[a-z][a-z0-9_-]*$/;

export const asWorkspaceTab = (value: string | null | undefined): WorkspaceTab | null => {
  if (!value) return null;
  const key = value.trim().toLowerCase();
  return SLUG.test(key) ? key : null;
};

export const resolveEnabledTabs = (app?: AppConfigSummary | null): WorkspaceTab[] => {
  const configured = Array.isArray(app?.enabled_tabs)
    ? app?.enabled_tabs?.map((tab) => asWorkspaceTab(tab)).filter((tab): tab is WorkspaceTab => Boolean(tab))
    : [];
  return configured.length > 0 ? configured : ALL_WORKSPACE_TABS;
};

// "needs_you" → "Needs you": the label of a sub-view the manifest names by key.
export const viewLabel = (key: string): string => {
  const words = key.replace(/[-_]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : key;
};

export const viewHref = (tab: WorkspaceTab, view: string, first: boolean): string =>
  first ? `/workspace?tab=${tab}` : `/workspace?tab=${tab}&view=${view}`;
