"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import React, { useEffect, useState } from "react";

import { HONEYPOT_VIEWS, asHoneypotView, honeypotViewHref } from "./honeypotViews";
import { PR_VIEWS, asPRView, prViewHref } from "./prViews";
import { COMPANY_NAME, LOGO_URL, SUPPORT_EMAIL, SUPPORT_WHATSAPP, isOpenEdition } from "../lib/edition";

type WorkspaceTab = "finops" | "audits" | "compliance" | "queue" | "maps" | "iac" | "prs" | "policies" | "settings" | "honeypots";
const ALL_WORKSPACE_TABS: WorkspaceTab[] = ["finops", "audits", "compliance", "queue", "maps", "iac", "prs", "policies", "settings", "honeypots"];

const asWorkspaceTab = (value: string | null): WorkspaceTab | null => {
  if (!value) return null;
  if ((ALL_WORKSPACE_TABS as string[]).includes(value)) {
    return value as WorkspaceTab;
  }
  return null;
};

type AccountInfo = {
  name: string | null;
  id: string | null;
};

type SettingsBootstrapSummary = {
  app?: {
    mode?: string | null;
    read_only?: boolean;
    settings_enabled?: boolean;
    production_url?: string | null;
    default_tab?: WorkspaceTab | null;
    enabled_tabs?: WorkspaceTab[] | null;
    disabled_features?: string[] | null;
    auth_enabled?: boolean;
  } | null;
  workspace?: {
    workspace_id?: string | null;
  } | null;
  billing?: {
    plan?: "free" | "cloud" | "enterprise" | null;
    audit_limit?: number | null;
    triage_limit?: number | null;
    fix_limit?: number | null;
    audits_used?: number | null;
    triages_used?: number | null;
    fixes_used?: number | null;
    audit_limit_reached?: boolean;
    triage_limit_reached?: boolean;
    fix_limit_reached?: boolean;
  } | null;
};

type TargetMetadata = {
  target?: string | null;
  account?: {
    id?: string | null;
    name?: string | null;
    alias?: string | null;
    account_id?: string | null;
  } | null;
  account_id?: string | null;
};

const pickFirstNonEmpty = (...values: Array<string | null | undefined>): string | null => {
  for (const value of values) {
    if (typeof value === "string") {
      const trimmed = value.trim();
      if (trimmed) {
        return trimmed;
      }
    }
  }
  return null;
};

export default function Sidebar({ onNavigate }: { onNavigate?: () => void } = {}) {
  const searchParams = useSearchParams();
  const tabParam = searchParams.get("tab");
  const requestedTab = tabParam === "radio" ? "audits" : asWorkspaceTab(tabParam);
  const honeypotView = asHoneypotView(searchParams.get("view"));
  const prView = asPRView(searchParams.get("view"));

  const linkBase = "block px-3 py-2 rounded transition";
  const activeCls = "bg-blue-100 text-blue-700 font-semibold";
  const inactiveCls = "text-gray-700 hover:bg-gray-100";
  const disabledCls = "cursor-not-allowed text-gray-400 bg-gray-50";

  const [accountInfo, setAccountInfo] = useState<AccountInfo | null>(null);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [appMode, setAppMode] = useState<string | null>(null);
  const [settingsEnabled, setSettingsEnabled] = useState(true);
  const [productionUrl, setProductionUrl] = useState<string | null>(null);
  const [billingPlan, setBillingPlan] = useState<"free" | "cloud" | "enterprise" | null>(null);
  const [triageLimit, setTriageLimit] = useState<number | null>(null);
  const [fixLimit, setFixLimit] = useState<number | null>(null);
  const [auditLimit, setAuditLimit] = useState<number | null>(null);
  const [auditsUsed, setAuditsUsed] = useState<number>(0);
  const [triagesUsed, setTriagesUsed] = useState<number>(0);
  const [fixesUsed, setFixesUsed] = useState<number>(0);
  const [upgradeRequired, setUpgradeRequired] = useState(false);
  const [defaultTab, setDefaultTab] = useState<WorkspaceTab>("finops");
  const [enabledTabs, setEnabledTabs] = useState<WorkspaceTab[]>([]);
  const [simpleAuthEnabled, setSimpleAuthEnabled] = useState(false);
  const [accountLoading, setAccountLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    const loadSidebarMetadata = async () => {
      try {
        const [targetResponse, settingsResponse] = await Promise.all([
          fetch("/api/target"),
          fetch("/api/settings/bootstrap"),
        ]);
        if (!targetResponse.ok) {
          throw new Error(`Failed to load target metadata (status ${targetResponse.status})`);
        }
        if (!settingsResponse.ok) {
          throw new Error(`Failed to load workspace metadata (status ${settingsResponse.status})`);
        }
        const data = (await targetResponse.json()) as TargetMetadata;
        const bootstrap = (await settingsResponse.json()) as SettingsBootstrapSummary;
        if (cancelled) return;

        const account = data?.account ?? null;
        const name = pickFirstNonEmpty(
          account?.name,
          account?.alias,
        );
        const id = pickFirstNonEmpty(
          account?.id,
          account?.account_id,
          data?.account_id,
        );
        setAccountInfo({ name: name ?? null, id: id ?? null });
        setWorkspaceId(bootstrap?.workspace?.workspace_id?.trim() || null);
        setAppMode(typeof bootstrap?.app?.mode === "string" ? bootstrap.app.mode : null);
        setSettingsEnabled(bootstrap?.app?.settings_enabled !== false);
        setSimpleAuthEnabled(bootstrap?.app?.auth_enabled === true);
        setProductionUrl(pickFirstNonEmpty(bootstrap?.app?.production_url));
        setBillingPlan(
          bootstrap?.billing?.plan === "free" || bootstrap?.billing?.plan === "cloud" || bootstrap?.billing?.plan === "enterprise"
            ? bootstrap.billing.plan
            : null,
        );
        setTriageLimit(
          typeof bootstrap?.billing?.triage_limit === "number"
            ? bootstrap.billing.triage_limit
            : null,
        );
        setFixLimit(
          typeof bootstrap?.billing?.fix_limit === "number"
            ? bootstrap.billing.fix_limit
            : null,
        );
        setAuditLimit(
          typeof bootstrap?.billing?.audit_limit === "number"
            ? bootstrap.billing.audit_limit
            : null,
        );
        setAuditsUsed(typeof bootstrap?.billing?.audits_used === "number" ? bootstrap.billing.audits_used : 0);
        setTriagesUsed(typeof bootstrap?.billing?.triages_used === "number" ? bootstrap.billing.triages_used : 0);
        setFixesUsed(typeof bootstrap?.billing?.fixes_used === "number" ? bootstrap.billing.fixes_used : 0);
        setUpgradeRequired(
          bootstrap?.billing?.audit_limit_reached === true ||
          bootstrap?.billing?.triage_limit_reached === true || bootstrap?.billing?.fix_limit_reached === true,
        );
        const nextDefaultTab = asWorkspaceTab(bootstrap?.app?.default_tab || null) || "finops";
        setDefaultTab(nextDefaultTab);
        const enabledFromBootstrap = Array.isArray(bootstrap?.app?.enabled_tabs)
          ? bootstrap.app.enabled_tabs.map((tab) => asWorkspaceTab(tab)).filter((tab): tab is WorkspaceTab => Boolean(tab))
          : [];
        setEnabledTabs(enabledFromBootstrap.length > 0 ? enabledFromBootstrap : ALL_WORKSPACE_TABS);
      } catch (error) {
        if (!cancelled) {
          console.error("Failed to load sidebar metadata", error);
          setAccountInfo({ name: null, id: null });
          setWorkspaceId(null);
          setAppMode(null);
          setSettingsEnabled(true);
          setProductionUrl(null);
          setBillingPlan(null);
          setTriageLimit(null);
          setFixLimit(null);
          setAuditLimit(null);
          setAuditsUsed(0);
          setTriagesUsed(0);
          setFixesUsed(0);
          setUpgradeRequired(false);
          setDefaultTab("finops");
          setEnabledTabs(ALL_WORKSPACE_TABS);
        }
      } finally {
        if (!cancelled) {
          setAccountLoading(false);
        }
      }
    };

    loadSidebarMetadata();
    const handleRefresh = () => {
      void loadSidebarMetadata();
    };
    window.addEventListener("beamreach:target-updated", handleRefresh);
    window.addEventListener("beamreach:queue-updated", handleRefresh);
    const intervalId = window.setInterval(handleRefresh, 15000);
    return () => {
      cancelled = true;
      window.removeEventListener("beamreach:target-updated", handleRefresh);
      window.removeEventListener("beamreach:queue-updated", handleRefresh);
      window.clearInterval(intervalId);
    };
  }, []);

  const isDemoMode = appMode === "demo";
  const requiresWorkspaceSetup =
    !isDemoMode && !accountLoading && !accountInfo?.name && !accountInfo?.id;
  const enabledTabSet = new Set(enabledTabs);
  const fallbackTab: WorkspaceTab = enabledTabSet.has(defaultTab) ? defaultTab : (enabledTabs[0] || "finops");
  const requestedOrDefault = requestedTab ?? fallbackTab;
  const workspaceSetupTab: WorkspaceTab = settingsEnabled && enabledTabSet.has("settings") ? "settings" : fallbackTab;
  const currentTabCandidate: WorkspaceTab = requiresWorkspaceSetup ? workspaceSetupTab : requestedOrDefault;
  const currentTab: WorkspaceTab = enabledTabSet.has(currentTabCandidate) ? currentTabCandidate : fallbackTab;
  type NavItem = { key: WorkspaceTab; label: string; href: string; disabled?: boolean };
  const item = (key: WorkspaceTab, label: string): NavItem => ({
    key,
    label,
    href: `/workspace?tab=${key}`,
    disabled: requiresWorkspaceSetup,
  });

  const SECURITY_TABS = new Set<WorkspaceTab>(["audits", "compliance", "honeypots"]);

  const rawGroups: NavItem[][] = [
    [item("finops", "FinOps"), item("iac", "IAC")],
    [item("audits", "DevSecOps"), item("compliance", "Compliance"), item("honeypots", "Honeypots")],
    [item("prs", "Pull requests"), item("maps", "Maps"), item("queue", "Queue")],
    [item("policies", "Policies")],
  ];

  if (settingsEnabled && enabledTabSet.has("settings")) {
    rawGroups[rawGroups.length - 1].push({ key: "settings", label: "Settings", href: "/workspace?tab=settings" });
  }

  const navGroups = rawGroups
    .map((group) => group.filter((i) => enabledTabSet.has(i.key)))
    .filter((group) => group.length > 0);

  return (
    <>
      {/* Header / Brand */}
      <div className="p-6 border-b">
        <Link href="/workspace" className="flex items-center gap-3">
          {LOGO_URL && <img src={LOGO_URL} alt={COMPANY_NAME} className="h-8 w-auto" />}
          <div className="flex flex-col leading-tight text-base font-semibold">
            {isOpenEdition ? <span>Compass</span> : (<><span>Beamreach</span><span>Compass</span></>)}
          </div>
        </Link>
      </div>
      {/* Nav */}
      <nav className="flex-1 p-4">
        <ul className="space-y-1 text-sm">
          {accountLoading ? (
            [60, 44, 52, 48].map((w, i) => (
              <li key={i}>
                <div className="h-8 rounded-lg bg-gray-100 animate-pulse" style={{ width: `${w}%` }} />
              </li>
            ))
          ) : navGroups.map((group, gi) => {
            // Label the group only when it has multiple items; a lone tab
            // (e.g. honeypots with audits/compliance disabled) needs no header.
            const isSecurityGroup = group.length > 1 && group.some((i) => SECURITY_TABS.has(i.key));
            return (
              <React.Fragment key={gi}>
                {gi > 0 && <li><hr className="my-2 border-gray-200" /></li>}
                {isSecurityGroup && (
                  <li className="px-3 pt-1 pb-0.5">
                    <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Security</span>
                  </li>
                )}
                {group.map((navItem) => {
                  const isActive = currentTab === navItem.key;
                  const className = `${linkBase} ${
                    isActive
                      ? activeCls
                      : navItem.disabled
                        ? disabledCls
                        : inactiveCls
                  }`;
                  // Honeypots and Pull requests are split into sub-pages; they show only while that tab is active.
                  const subItems: Array<{ key: string; label: string; href: string; active: boolean }> =
                    isActive && !navItem.disabled && navItem.key === "honeypots"
                      ? HONEYPOT_VIEWS.map((v) => ({ key: v.key, label: v.label, href: honeypotViewHref(v.key), active: honeypotView === v.key }))
                      : isActive && !navItem.disabled && navItem.key === "prs"
                        ? PR_VIEWS.map((v) => ({ key: v.key, label: v.label, href: prViewHref(v.key), active: prView === v.key }))
                        : [];
                  return (
                    <li key={navItem.key}>
                      {navItem.disabled ? (
                        <span aria-disabled="true" className={className}>{navItem.label}</span>
                      ) : (
                        <Link href={navItem.href} className={className} onClick={onNavigate}>{navItem.label}</Link>
                      )}
                      {subItems.length > 0 && (
                        <ul className="ml-4 mt-1 space-y-0.5 border-l border-gray-200 pl-2" aria-label={`${navItem.label} sections`}>
                          {subItems.map((view) => {
                            const viewActive = view.active;
                            return (
                              <li key={view.key}>
                                <Link
                                  href={view.href}
                                  onClick={onNavigate}
                                  aria-current={viewActive ? "page" : undefined}
                                  className={`block rounded px-2 py-1 text-[13px] transition ${
                                    viewActive ? "bg-blue-50 font-semibold text-blue-700" : "text-gray-600 hover:bg-gray-100"
                                  }`}
                                >
                                  {view.label}
                                </Link>
                              </li>
                            );
                          })}
                        </ul>
                      )}
                    </li>
                  );
                })}
              </React.Fragment>
            );
          })}
        </ul>
      </nav>

      {/* Bottom actions */}
      <div className="p-4 border-t space-y-2">
        <div className="text-xs text-gray-600">
          <div className="uppercase text-[10px] font-semibold tracking-wide text-gray-500">Target account</div>
          <div className="text-sm font-semibold text-gray-900">
            {accountLoading ? "Loading…" : accountInfo?.name || "Unknown"}
          </div>
          <div className="font-mono text-[11px] text-gray-500">
            {accountLoading ? "Loading…" : accountInfo?.id || "—"}
          </div>
          <div className="mt-3 uppercase text-[10px] font-semibold tracking-wide text-gray-500">Workspace ID</div>
          <div className="font-mono text-[11px] text-gray-500 break-all">
            {accountLoading ? "Loading…" : workspaceId || "—"}
          </div>
          {!accountLoading && billingPlan !== null && (
            <div className="mt-3 pt-3 border-t border-gray-200">
              <div className="uppercase text-[10px] font-semibold tracking-wide text-gray-500">Usage limits</div>
              <div className="text-xs text-gray-700">
                {auditLimit === null ? "Audits: Unlimited" : `Audits: ${auditsUsed} / ${auditLimit}`}
              </div>
              <div className="text-xs text-gray-700">
                {triageLimit === null ? "Triages: Unlimited" : `Triages: ${triagesUsed} / ${triageLimit}`}
              </div>
              <div className="text-xs text-gray-700">
                {fixLimit === null ? "Fixes: Unlimited" : `Fixes: ${fixesUsed} / ${fixLimit}`}
              </div>
            </div>
          )}
        </div>
        <button
          onClick={async () => {
            if (simpleAuthEnabled) {
              await fetch("/api/auth/logout", { method: "POST" }).catch(() => {});
              window.location.href = "/login";
            } else {
              window.location.href = "/logout";
            }
          }}
          className="block w-full text-center rounded bg-gray-900 text-white px-3 py-2 text-sm hover:opacity-90"
        >
          Logout
        </button>
        {isDemoMode && productionUrl && (
          <a
            href={productionUrl}
            className="block w-full text-center rounded border border-gray-300 bg-white px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
          >
            Self-host on AWS
          </a>
        )}
        {SUPPORT_WHATSAPP && <div className="pt-2 border-t border-gray-100">
          <a
            href={`https://wa.me/${SUPPORT_WHATSAPP}?text=${encodeURIComponent(
              `Hi, I'm using Beamreach Compass${workspaceId ? ` (workspace: ${workspaceId})` : ""}. I'd like to share some feedback.`
            )}`}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center justify-center gap-2 w-full rounded border border-green-600 text-green-700 px-3 py-2 text-sm font-medium hover:bg-green-50 transition"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4 fill-current" aria-hidden="true">
              <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z"/>
            </svg>
            Talk to the founder
          </a>
          {SUPPORT_EMAIL && <p className="mt-1.5 text-center text-[10px] text-gray-400">
            No WhatsApp?{" "}
            <a
              href={`mailto:${SUPPORT_EMAIL}?subject=Beamreach Compass feedback${workspaceId ? ` — ${workspaceId}` : ""}`}
              className="underline hover:text-gray-600"
            >
              Email instead
            </a>
          </p>}
        </div>}
      </div>
    </>
  );
}
