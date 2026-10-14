// The dashboard tabs, in one place. The union must be a closed set because the
// tab in the URL and `enabled_tabs` from /api/settings/bootstrap are parsed
// against it, so product tab keys are listed here even though their views only
// exist when the product registers them (app/lib/extensions.ts). The API's
// counterpart is compass_core.features._TAB_FEATURES.
export type WorkspaceTab = "finops" | "audits" | "compliance" | "queue" | "maps" | "iac" | "prs" | "policies" | "settings" | "honeypots";
export type DashboardTab = "radio" | WorkspaceTab;
export const ALL_WORKSPACE_TABS: WorkspaceTab[] = ["finops", "audits", "compliance", "queue", "maps", "iac", "prs", "policies", "settings", "honeypots"];

// Legacy URLs: `?tab=radio` has always opened the audits tab.
export const TAB_ALIASES: Partial<Record<string, WorkspaceTab>> = { radio: "audits" };

export type AppConfigSummary = {
  mode?: "production" | "demo" | null;
  read_only?: boolean;
  settings_enabled?: boolean;
  default_tab?: WorkspaceTab | null;
  enabled_tabs?: WorkspaceTab[] | null;
  disabled_features?: string[] | null;
  production_url?: string | null;
};

export const asWorkspaceTab = (value: string | null | undefined): WorkspaceTab | null => {
  if (!value) return null;
  return (ALL_WORKSPACE_TABS as string[]).includes(value) ? (value as WorkspaceTab) : null;
};

export const resolveEnabledTabs = (app?: AppConfigSummary | null): WorkspaceTab[] => {
  const configured = Array.isArray(app?.enabled_tabs)
    ? app?.enabled_tabs?.map((tab) => asWorkspaceTab(tab)).filter((tab): tab is WorkspaceTab => Boolean(tab))
    : [];
  return configured.length > 0 ? configured : ALL_WORKSPACE_TABS;
};
