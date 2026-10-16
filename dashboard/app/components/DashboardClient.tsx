"use client";
// The dashboard shell: the workspace bootstrap, the tab in the URL, the
// infra-scan banner, the open tabs (maps, IAC, pull requests, honeypots, queue)
// and the open Settings sections. The product's tabs, settings sections and
// banners plug in through app/lib/extensions.ts and read the shell's state
// with useShell() (app/lib/shell.tsx).
import React, { useCallback, useEffect, useMemo, useState, useRef } from "react";
import { useSearchParams } from "next/navigation";
import axios from "axios";
import { COMPANY_NAME, isOpenEdition } from "../lib/edition";
import { openTabs } from "../lib/captains";
import { productBanners, productProviders, productSettingsSections, productTabs } from "../lib/extensions";
import type { SettingsSlot } from "../lib/extensions";
import { ShellProvider } from "../lib/shell";
import CaptainsSection from "./CaptainsSection";
import CompassSection, { UpdateBanner } from "./CompassUpdates";
import MappingProgressBar from "./MappingProgressBar";
import { lastScanDurationMs } from "./mappingProgress";
import GenericCaptainTab from "./GenericCaptainTab";
import type { QueueEvent, QueueJob, SettingsBootstrap, SettingsSaveParticipant, Shell } from "../lib/shell";
import { ALL_WORKSPACE_TABS, SETTINGS_VIEWS, TAB_ALIASES, asWorkspaceTab, resolveEnabledTabs, settingsSectionId } from "../lib/tabs";
import type { DashboardTab, WorkspaceTab } from "../lib/tabs";
import SlackIntegrationCard from "./SlackIntegrationCard";

const resolveTab = (
  params: ReturnType<typeof useSearchParams>,
  canAccessProtectedTabs: boolean | null,
  defaultTab: WorkspaceTab = "finops",
  settingsEnabled = true,
  enabledTabs: WorkspaceTab[] = ALL_WORKSPACE_TABS,
): DashboardTab => {
  const enabled = new Set(enabledTabs);
  const fallbackTab: WorkspaceTab = enabled.has(defaultTab) ? defaultTab : (enabledTabs[0] || "finops");
  if (canAccessProtectedTabs === false) {
    if (settingsEnabled && enabled.has("settings")) {
      return "settings";
    }
    return fallbackTab;
  }
  const requested = params?.get("tab") || null;
  const tab = (requested && TAB_ALIASES[requested]) || requested;
  const workspaceTab = asWorkspaceTab(tab);
  if (workspaceTab) {
    if (workspaceTab === "settings" && !settingsEnabled) {
      return fallbackTab;
    }
    if (enabled.has(workspaceTab)) {
      return workspaceTab;
    }
    return fallbackTab;
  }
  return fallbackTab;
};

// One of the Settings page's sections (lib/tabs.ts SETTINGS_VIEWS): the heading the
// sidebar's sub-link scrolls to.
function SettingsHeading({ view, title, children }: { view: string; title: string; children?: React.ReactNode }) {
  return (
    <div id={settingsSectionId(view)} className="scroll-mt-6 border-b border-gray-200 pb-2 pt-4">
      <h2 className="text-xl font-semibold text-gray-900">{title}</h2>
      {children && <p className="mt-1 text-sm text-gray-600">{children}</p>}
    </div>
  );
}

// The product's settings sections registered for one position among the open ones.
function SettingsSections({ slot }: { slot: SettingsSlot }) {
  return (
    <>
      {productSettingsSections
        .filter((section) => section.slot === slot)
        .map(({ key, Component }) => <Component key={key} />)}
    </>
  );
}

export default function DashboardClient() {
  const searchParams = useSearchParams();
  const [settingsBootstrap, setSettingsBootstrap] = useState<SettingsBootstrap | null>(null);
  const [settingsLoading, setSettingsLoading] = useState(true);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [targetConfigured, setTargetConfigured] = useState<boolean | null>(null);
  const [tabResolved, setTabResolved] = useState(false);
  const [awsMode, setAwsMode] = useState<"assume_role" | "access_key" | "local_iam">("assume_role");
  const [awsRoleAccountId, setAwsRoleAccountId] = useState("");
  const [awsRoleName, setAwsRoleName] = useState("");
  const [awsExternalId, setAwsExternalId] = useState("");
  const [awsAccessKeyId, setAwsAccessKeyId] = useState("");
  const [awsSecretAccessKey, setAwsSecretAccessKey] = useState("");
  const [awsSessionToken, setAwsSessionToken] = useState("");
  const [awsDirty, setAwsDirty] = useState(false);
  const awsDirtyRef = useRef(false);
  const [showDifferentAccount, setShowDifferentAccount] = useState(false);
  const [awsSaving, setAwsSaving] = useState(false);
  const [awsTesting, setAwsTesting] = useState(false);
  const [awsSuccess, setAwsSuccess] = useState<string | null>(null);
  const [awsError, setAwsError] = useState<string | null>(null);
  const [githubMessage, setGithubMessage] = useState<string | null>(null);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsSaveSuccess, setSettingsSaveSuccess] = useState<string | null>(null);
  const [saveParticipants, setSaveParticipants] = useState<Record<string, SettingsSaveParticipant>>({});
  const [queueJobs, setQueueJobs] = useState<QueueJob[]>([]);
  const [queueError, setQueueError] = useState<string | null>(null);

  const [activeTab, setActiveTab] = useState<DashboardTab>(resolveTab(searchParams, null));

  useEffect(() => {
    const enabledTabs = resolveEnabledTabs(settingsBootstrap?.app);
    const preferredDefault = asWorkspaceTab(settingsBootstrap?.app?.default_tab || null) || "finops";
    const defaultTab = enabledTabs.includes(preferredDefault) ? preferredDefault : (enabledTabs[0] || "finops");
    const settingsEnabled = settingsBootstrap?.app?.settings_enabled !== false && enabledTabs.includes("settings");
    setActiveTab(resolveTab(searchParams, targetConfigured, defaultTab, settingsEnabled, enabledTabs));
    setTabResolved(true);
  }, [
    searchParams,
    settingsBootstrap?.app?.default_tab,
    settingsBootstrap?.app?.enabled_tabs,
    settingsBootstrap?.app?.settings_enabled,
    targetConfigured,
  ]);

  const applySettingsBootstrap = useCallback((bootstrap: SettingsBootstrap) => {
    setSettingsBootstrap(bootstrap);
    // local_iam: the account is whatever the credentials belong to; configured is enough.
    const canAccessTabs =
      bootstrap.app?.mode === "demo" ||
      Boolean(bootstrap.aws?.account_id) ||
      (bootstrap.aws?.mode === "local_iam" && bootstrap.aws?.configured === true);
    setTargetConfigured(canAccessTabs);

    if (!awsDirtyRef.current) {
      const nextMode =
        bootstrap.aws?.mode === "access_key"
          ? "access_key"
          : bootstrap.aws?.mode === "local_iam"
            ? "local_iam"
            : "assume_role";
      setAwsMode(nextMode);
      setShowDifferentAccount(nextMode !== "local_iam" && Boolean(bootstrap.aws?.configured));
      setAwsRoleAccountId(bootstrap.aws?.account_id || "");
      setAwsRoleName(bootstrap.aws?.role_name || bootstrap.aws?.suggested_role_name || "BeamreachCompassRole");
      setAwsExternalId(bootstrap.aws?.external_id || "");
      setAwsAccessKeyId("");
      setAwsSecretAccessKey("");
      setAwsSessionToken("");
      setAwsDirty(false);
    }
  }, []);

  const markAwsDirty = useCallback(() => {
    awsDirtyRef.current = true;
    setAwsDirty(true);
    setAwsError(null);
    setAwsSuccess(null);
    setSettingsSaveSuccess(null);
  }, []);

  const refreshSettingsBootstrap = useCallback(async () => {
    setSettingsLoading(true);
    setSettingsError(null);
    try {
      const response = await axios.get("/api/settings/bootstrap");
      applySettingsBootstrap(response.data as SettingsBootstrap);
    } catch (error: any) {
      if (error?.response?.status === 401) {
        window.location.href = "/login";
        return;
      }
      const detail = error?.response?.data?.detail || error?.message || "Failed to load workspace settings";
      setSettingsError(detail);
    } finally {
      setSettingsLoading(false);
    }
  }, [applySettingsBootstrap]);

  useEffect(() => {
    void refreshSettingsBootstrap();
  }, [refreshSettingsBootstrap]);

  useEffect(() => {
    const githubStatus = searchParams?.get("github");
    if (githubStatus === "connected") {
      setGithubMessage("GitHub is connected.");
      void refreshSettingsBootstrap();
    }
  }, [searchParams, refreshSettingsBootstrap]);

  const silentRefreshSettingsBootstrap = useCallback(async () => {
    try {
      const response = await axios.get("/api/settings/bootstrap");
      applySettingsBootstrap(response.data as SettingsBootstrap);
    } catch {
      // silent — background poll, don't flash errors
    }
  }, [applySettingsBootstrap]);

  useEffect(() => {
    if (settingsBootstrap?.infra_scan?.scanned !== false) return;
    const interval = window.setInterval(() => {
      void silentRefreshSettingsBootstrap();
    }, 5000);
    return () => window.clearInterval(interval);
  }, [silentRefreshSettingsBootstrap, settingsBootstrap?.infra_scan?.scanned]);

  const beamreachAwsAccountId = settingsBootstrap?.aws?.beamreach_account_id || "";
  const isReadOnly = settingsBootstrap?.app?.read_only === true;
  const enabledWorkspaceTabs = resolveEnabledTabs(settingsBootstrap?.app);
  const queueEnabled = enabledWorkspaceTabs.includes("queue");
  const settingsEnabled = settingsBootstrap?.app?.settings_enabled !== false && enabledWorkspaceTabs.includes("settings");
  const disabledFeaturesSource = settingsBootstrap?.app?.disabled_features;
  const disabledFeatures = useMemo<string[]>(
    () => (Array.isArray(disabledFeaturesSource) ? disabledFeaturesSource : []),
    [disabledFeaturesSource],
  );
  const awsIntegrationEnabled = !disabledFeatures.includes("aws_integration");
  const githubIntegrationEnabled = !disabledFeatures.includes("github_integration");
  const productionUrl = settingsBootstrap?.app?.production_url || "";
  const infraScan = settingsBootstrap?.infra_scan || null;
  const infraScanned = infraScan?.scanned === true;
  const infraScanIntegrationsReady = infraScan?.integrations_ready === true;
  const effectiveRoleName =
    awsRoleName.trim() || settingsBootstrap?.aws?.suggested_role_name || "BeamreachCompassRole";
  const externalIdForSetup = awsExternalId.trim() || settingsBootstrap?.aws?.external_id || "";
  const externalIdDisplay = externalIdForSetup || (settingsLoading ? "Generating..." : "");
  const terraformRoleSnippet = useMemo(
    () =>
      `resource "aws_iam_role" "beamreach_compass" {
  name = "${effectiveRoleName}"

  # ${COMPANY_NAME} assumes this role from account ${beamreachAwsAccountId}.
  # In production that call comes from the ECS task role in that account.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        AWS = "arn:aws:iam::${beamreachAwsAccountId}:root"
      }
      Action = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "sts:ExternalId" = "${externalIdForSetup}"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "beamreach_compass_readonly" {
  role       = aws_iam_role.beamreach_compass.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}`,
    [beamreachAwsAccountId, effectiveRoleName, externalIdForSetup],
  );
  const cloudFormationRoleSnippet = useMemo(
    () =>
      `Resources:
  BeamreachCompassRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: ${effectiveRoleName}
      # ${COMPANY_NAME} assumes this role from account ${beamreachAwsAccountId}.
      # In production that call comes from the ECS task role in that account.
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Principal:
              AWS: arn:aws:iam::${beamreachAwsAccountId}:root
            Action: sts:AssumeRole
            Condition:
              StringEquals:
                sts:ExternalId: ${externalIdForSetup}
      ManagedPolicyArns:
        - arn:aws:iam::aws:policy/ReadOnlyAccess`,
    [beamreachAwsAccountId, effectiveRoleName, externalIdForSetup],
  );

  const buildAwsPayload = () =>
    awsMode === "local_iam"
      ? { mode: "local_iam" as const }
      : awsMode === "assume_role"
        ? {
            mode: "assume_role" as const,
            assume_role: {
              account_id: awsRoleAccountId.trim(),
              role_name: awsRoleName.trim(),
              external_id: awsExternalId.trim() || null,
            },
          }
        : {
            mode: "access_key" as const,
            access_key: {
              access_key_id: awsAccessKeyId.trim(),
              secret_access_key: awsSecretAccessKey.trim(),
              session_token: awsSessionToken.trim() || null,
            },
          };

  const saveAwsSettings = useCallback(
    async (options?: { showSuccess?: boolean }) => {
      if (awsSaving) return false;
      setAwsSaving(true);
      setAwsError(null);
      setAwsSuccess(null);
      setGithubMessage(null);

      try {
        const response = await axios.post("/api/settings/aws", buildAwsPayload());
        const bootstrapResponse = await axios.get("/api/settings/bootstrap");
        awsDirtyRef.current = false;
        applySettingsBootstrap(bootstrapResponse.data as SettingsBootstrap);
        const finopsJobId =
          typeof response.data?.audit?.finops?.job_id === "string" ? response.data.audit.finops.job_id : null;
        const securityJobId =
          typeof response.data?.audit?.security?.job_id === "string" ? response.data.audit.security.job_id : null;
        if (options?.showSuccess !== false) {
          if (finopsJobId || securityJobId) {
            const queued = [
              finopsJobId ? `FinOps ${finopsJobId}` : null,
              securityJobId ? `Security ${securityJobId}` : null,
            ].filter(Boolean).join(", ");
            setAwsSuccess(`AWS settings saved. Audits queued (${queued}).`);
          } else {
            setAwsSuccess("AWS settings saved.");
          }
        }
        window.dispatchEvent(new Event("beamreach:queue-updated"));
        window.dispatchEvent(new Event("beamreach:target-updated"));

        const accountId = response.data?.aws?.account_id;
        if (typeof accountId === "string" && accountId.trim()) {
          setAwsRoleAccountId(accountId);
        }
        return { ok: true, finopsJobId, securityJobId };
      } catch (error: any) {
        const detail = error?.response?.data?.detail || error?.message || "Failed to save AWS settings";
        setAwsError(detail);
        throw error;
      } finally {
        setAwsSaving(false);
      }
    },
    [applySettingsBootstrap, awsSaving, awsAccessKeyId, awsMode, awsRoleAccountId, awsRoleName, awsSecretAccessKey, awsSessionToken, awsExternalId],
  );

  const handleTestAwsSettings = async () => {
    if (awsTesting || settingsLoading) return;
    setAwsTesting(true);
    setAwsError(null);
    setAwsSuccess(null);
    setSettingsSaveSuccess(null);
    try {
      const response = await axios.post("/api/settings/aws/test", buildAwsPayload());
      const accountId = response.data?.aws?.account_id;
      const callerArn = response.data?.aws?.caller_arn;
      setAwsSuccess(
        accountId
          ? `AWS access test succeeded for account ${accountId}${callerArn ? ` (${callerArn})` : ""}.`
          : "AWS access test succeeded.",
      );
    } catch (error: any) {
      const raw = error?.response?.data?.detail;
      const detail = typeof raw === "string" ? raw : (error?.message || "Failed to validate AWS access");
      setAwsError(detail);
    } finally {
      setAwsTesting(false);
    }
  };

  // The sections saved by the "Save settings" bar besides AWS access (the product's
  // Bedrock models, say), registered through useSettingsSaveParticipant.
  const participants = Object.values(saveParticipants);
  const participantsDirty = participants.some((participant) => participant.dirty);
  const participantsLoading = participants.some((participant) => participant.loading);
  const participantsSaving = participants.some((participant) => participant.saving);

  const handleSaveSettings = async () => {
    if (settingsSaving || settingsLoading || awsTesting || participantsLoading || (!awsDirty && !participantsDirty)) return;
    setSettingsSaving(true);
    setSettingsSaveSuccess(null);
    try {
      // What a section shows may depend on the AWS credentials (model
      // availability), so a section that had nothing to save refreshes instead.
      const refreshAfterAwsSave = awsDirty && !participantsDirty;
      let finopsJobId: string | null = null;
      let securityJobId: string | null = null;
      if (awsDirty) {
        const awsSaveResult = await saveAwsSettings({ showSuccess: false });
        finopsJobId = awsSaveResult && typeof awsSaveResult === "object" && "finopsJobId" in awsSaveResult
          ? (awsSaveResult.finopsJobId as string | null)
          : null;
        securityJobId = awsSaveResult && typeof awsSaveResult === "object" && "securityJobId" in awsSaveResult
          ? (awsSaveResult.securityJobId as string | null)
          : null;
      }
      for (const participant of participants) {
        if (participant.dirty) {
          await participant.save();
        } else if (refreshAfterAwsSave) {
          await participant.afterAwsSaved?.();
        }
      }
      if (finopsJobId || securityJobId) {
        const queued = [
          finopsJobId ? `FinOps ${finopsJobId}` : null,
          securityJobId ? `Security ${securityJobId}` : null,
        ].filter(Boolean).join(", ");
        setSettingsSaveSuccess(`Settings saved. Audits queued (${queued}).`);
      } else {
        setSettingsSaveSuccess("Settings saved.");
      }
    } finally {
      setSettingsSaving(false);
    }
  };

  const handleConnectGithub = () => {
    setGithubMessage(null);
    window.location.href = "/api/integrations/github/connect";
  };

  const fetchQueueJobs = useCallback(async () => {
    if (!queueEnabled) {
      setQueueJobs([]);
      setQueueError(null);
      return;
    }
    try {
      const res = await axios.get("/api/jobs?limit=100");
      const raw = Array.isArray(res.data)
        ? res.data
        : Array.isArray(res.data?.items)
          ? res.data.items
          : [];
      const normalized: QueueJob[] = raw
        .map((job: any) => {
          const created = Number(job?.created_at);
          const updated = Number(job?.updated_at ?? job?.created_at);
          const jobId = job?.job_id ?? "";
          if (!jobId) return null;
          const eventsRaw = Array.isArray(job?.events) ? job.events : [];
          const events: QueueEvent[] = eventsRaw
            .map((evt: any) => {
              const ts = Number(evt?.timestamp);
              const msg = evt?.message;
              if (!msg) return null;
              return {
                timestamp: Number.isFinite(ts) ? ts : 0,
                message: String(msg),
                type: typeof evt?.type === "string" ? evt.type : "info",
                status: typeof evt?.status === "string" ? evt.status : null,
                data: evt?.data ?? null,
              } as QueueEvent;
            })
            .filter((evt: QueueEvent | null): evt is QueueEvent => Boolean(evt))
            .sort((a: QueueEvent, b: QueueEvent) => (a.timestamp || 0) - (b.timestamp || 0));
          return {
            job_id: jobId,
            captain: job?.captain || "unknown",
            command: job?.command || "unknown",
            status: job?.status || "queued",
            queue: job?.queue || "captain-executions",
            session_id: job?.session_id ?? null,
            result_summary: job?.result_summary ?? null,
            error: job?.error ?? null,
            created_at: Number.isFinite(created) ? created : 0,
            updated_at: Number.isFinite(updated) ? updated : (Number.isFinite(created) ? created : 0),
            events,
          } as QueueJob;
        })
        .filter((job: QueueJob | null): job is QueueJob => Boolean(job));
      normalized.sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
      setQueueJobs(normalized);
      setQueueError(null);
      void refreshSettingsBootstrap();
    } catch (error) {
      console.error("Failed to load queue jobs", error);
      setQueueError("Failed to load queue jobs");
    }
  }, [queueEnabled, refreshSettingsBootstrap]);

  useEffect(() => {
    fetchQueueJobs();
  }, [fetchQueueJobs]);

  useEffect(() => {
    const refreshQueueJobs = () => {
      void fetchQueueJobs();
    };
    window.addEventListener("beamreach:queue-updated", refreshQueueJobs);
    return () => {
      window.removeEventListener("beamreach:queue-updated", refreshQueueJobs);
    };
  }, [fetchQueueJobs]);

  const activeMapperJob = useMemo(
    () =>
      queueJobs.find((job) => {
        const status = (job.status || "").toLowerCase();
        return job.captain === "mapper" && job.command === "run" && (status === "queued" || status === "running" || status === "retrying");
      }) || null,
    [queueJobs],
  );
  const infraScanBlocked = infraScanIntegrationsReady && !infraScanned;
  const infraScanStatusLabel = (activeMapperJob?.status || infraScan?.status || "").toLowerCase();
  const latestMapperEvent = useMemo(
    () => (activeMapperJob?.events?.length ? activeMapperJob.events[activeMapperJob.events.length - 1] : null),
    [activeMapperJob],
  );
  // a running scan's events are refreshed every five seconds on every tab (the Queue tab polls on its own)
  useEffect(() => {
    if (!activeMapperJob || activeTab === "queue") return;
    const poll = window.setInterval(() => void fetchQueueJobs(), 5000);
    return () => window.clearInterval(poll);
  }, [activeMapperJob, activeTab, fetchQueueJobs]);
  // Settings is one page: ?view=<section> scrolls to that section's heading
  const settingsView = searchParams?.get("view") || null;
  useEffect(() => {
    if (activeTab !== "settings") return;
    const views = SETTINGS_VIEWS(isOpenEdition);
    if (!settingsView || settingsView === views[0]) {
      window.scrollTo({ top: 0, behavior: "smooth" });
      return;
    }
    if (!views.includes(settingsView)) return;
    // the sections render after the bootstrap; try again briefly until the heading is there
    let tries = 0;
    const find = window.setInterval(() => {
      const el = document.getElementById(settingsSectionId(settingsView));
      if (el || ++tries > 20) {
        window.clearInterval(find);
        el?.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }, 100);
    return () => window.clearInterval(find);
  }, [activeTab, settingsView]);

  const previousScanMs = useMemo(() => lastScanDurationMs(queueJobs, activeMapperJob?.job_id), [queueJobs, activeMapperJob?.job_id]);
  // the Maps tab shows a running scan in its own scan panel; every other tab in the banner
  const infraScanBannerVisible =
    !(activeTab === "maps" && activeMapperJob) && ((infraScanIntegrationsReady && !infraScanned) || Boolean(activeMapperJob));
  const infraScanBannerCopy = useMemo(() => {
    if (!infraScanBannerVisible) return null;
    if (infraScanStatusLabel === "failed") {
      return "Infra scan failed. Retry it from Maps → Run mapping; what depends on the map shows once a scan completes.";
    }
    // What this tab will show once the first full scan has completed.
    const afterScan: Partial<Record<string, string>> = {
      honeypots: "Decoy suggestions — where a honeytoken would look real, next to your actual resources — appear when it completes. You can plant decoys by hand now.",
      iac: "Resources that are not in Terraform, and drift, appear when it completes.",
      maps: "The map fills in when it completes.",
      prs: "Pull requests Compass opens show here as they are created.",
      queue: "The scan is the running job below.",
      finops: "Triage and fixes unlock when it completes. Findings are available now.",
      audits: "Triage and fixes unlock when it completes. Findings are available now.",
    };
    if (activeMapperJob && (infraScanned || infraScan?.last_completed_at)) {
      return "Mapping your account. The map updates when the scan completes; until then it shows the last scan.";
    }
    const lead = activeMapperJob ? "Mapping your account (first full scan)." : "The first full scan of your account has not completed yet.";
    const detail = afterScan[activeTab] || "Features that depend on the map unlock when it completes.";
    return `${lead} ${detail}`;
  }, [activeMapperJob, activeTab, infraScanBannerVisible, infraScanStatusLabel, infraScanned, infraScan?.last_completed_at]);

  useEffect(() => {
    if (activeTab !== "queue") return;
    fetchQueueJobs();
    const interval = setInterval(fetchQueueJobs, 5000);
    return () => clearInterval(interval);
  }, [activeTab, fetchQueueJobs]);

  const registerSettingsSaveParticipant = useCallback((id: string, participant: SettingsSaveParticipant) => {
    setSaveParticipants((prev) => ({ ...prev, [id]: participant }));
  }, []);
  const unregisterSettingsSaveParticipant = useCallback((id: string) => {
    setSaveParticipants((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);
  const clearSettingsSaveSuccess = useCallback(() => setSettingsSaveSuccess(null), []);

  const shell: Shell = {
    activeTab,
    searchParams,
    settingsBootstrap,
    settingsError,
    setSettingsBootstrap,
    refreshSettingsBootstrap,
    disabledFeatures,
    infraScan,
    infraScanned,
    infraScanBlocked,
    queueJobs,
    queueError,
    fetchQueueJobs,
    activeMapperJob,
    registerSettingsSaveParticipant,
    unregisterSettingsSaveParticipant,
    clearSettingsSaveSuccess,
  };

  // The component for the active key, from the open captains' registry and the
  // product's, if the API did not hide the tab (enabled_tabs already applies the
  // feature flags and the captains' states; the feature check mirrors it for a
  // bootstrap that lists tabs but not features).
  const activeTabDef = [...openTabs, ...productTabs].find(
    (tab) =>
      tab.key === activeTab &&
      (tab.key === "radio" || enabledWorkspaceTabs.includes(tab.key)) &&
      !disabledFeatures.includes(tab.feature),
  );

  const body = (
    <div className="flex max-w-screen-xl mx-auto">
      <main className="min-w-0 flex-1 p-4 md:p-8">
        {isReadOnly && (
          <div className="mb-6 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
            Demo mode is read-only. Findings and reports are visible, but configuration and write actions are disabled.
            {productionUrl ? (
              <>
                {" "}
                <a href={productionUrl} className="font-semibold underline underline-offset-2">
                  Register in production
                </a>
                .
              </>
            ) : null}
          </div>
        )}
        {productBanners.map((Banner, index) => (
          <Banner key={index} />
        ))}
        {isOpenEdition && (
          <UpdateBanner update={settingsBootstrap?.update} settingsHref="/workspace?tab=settings&view=updates" />
        )}
        {infraScanBannerVisible && (
          <div
            className={`mb-6 rounded-lg px-4 py-3 text-sm ${
              infraScanStatusLabel === "failed"
                ? "border border-red-200 bg-red-50 text-red-900"
                : "border border-sky-200 bg-sky-50 text-sky-900"
            }`}
          >
            <div className="font-medium">{infraScanBannerCopy}</div>
            {activeMapperJob ? (
              <>
                <MappingProgressBar job={activeMapperJob} previousMs={previousScanMs} />
                {latestMapperEvent?.message && <div className="mt-1 text-[11px] opacity-60">{latestMapperEvent.message}</div>}
              </>
            ) : (
              (infraScan?.latest_message || infraScan?.last_error) && (
                <div className="mt-1 text-xs opacity-80">
                  {infraScan.latest_message || infraScan.last_error}
                </div>
              )
            )}
          </div>
        )}
        {!tabResolved ? (
          <div className="flex items-center justify-center py-20 text-gray-400 text-sm">Loading…</div>
        ) : activeTab === "settings" ? (
          settingsEnabled ? (
          <div className="space-y-6">
            <div>
              <h1 className="text-2xl font-bold mb-2">Settings</h1>
              <p className="text-sm text-gray-600">Manage automation behaviour for Compass.</p>
            </div>

            {(!settingsBootstrap?.aws?.configured || !settingsBootstrap?.github?.connected) && (
              <div className="rounded-lg border border-amber-300 bg-amber-50 px-5 py-4">
                <div className="flex items-start gap-3">
                  <svg className="mt-0.5 h-5 w-5 flex-shrink-0 text-amber-500" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                    <path fillRule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 5a.75.75 0 01.75.75v3.5a.75.75 0 01-1.5 0v-3.5A.75.75 0 0110 5zm0 9a1 1 0 100-2 1 1 0 000 2z" clipRule="evenodd" />
                  </svg>
                  <div className="space-y-1">
                    <p className="text-sm font-semibold text-amber-900">Complete setup to start using Compass</p>
                    <p className="text-sm text-amber-800">
                      Compass needs access to your AWS account and GitHub to begin scanning and triaging findings.
                      Configure the highlighted sections below to get started.
                    </p>
                  </div>
                </div>
              </div>
            )}

            <SettingsSections slot="top" />

            <SettingsHeading view="integrations" title="Integrations">
              The AWS account Compass maps, the GitHub it opens pull requests on, and where it sends alerts.
            </SettingsHeading>

            {awsIntegrationEnabled && <section className={`bg-white border rounded-lg shadow-sm p-6 space-y-4 ${!settingsBootstrap?.aws?.configured ? "border-amber-400 ring-2 ring-amber-300" : ""}`}>
              <div className="space-y-1">
                <h2 className="text-lg font-semibold">AWS access {!settingsBootstrap?.aws?.configured && <span className="ml-2 inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-800 border border-amber-300">Required</span>}</h2>
                <p className="text-sm text-gray-600">
                  The target account is the AWS account Compass will scan and optimize. Choose which account to connect to.
                </p>
              </div>
              {awsError && (
                <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                  {awsError}
                </div>
              )}
              {awsSuccess && (
                <div className="rounded border border-green-200 bg-green-50 px-3 py-2 text-sm text-green-700">
                  {awsSuccess}
                </div>
              )}
              <button
                type="button"
                onClick={() => {
                  markAwsDirty();
                  setAwsMode("local_iam");
                  setShowDifferentAccount(false);
                }}
                className={`w-full text-left rounded-md px-4 py-3 text-sm font-semibold transition ${
                  awsMode === "local_iam"
                    ? "bg-black text-white"
                    : "bg-blue-600 text-white hover:bg-black"
                }`}
              >
                <div className="flex items-center justify-between gap-4">
                  <div className="space-y-0.5">
                    <div className="font-semibold">
                      Target the same account Compass is deployed in
                      {beamreachAwsAccountId && (
                        <span className="ml-2 font-mono font-normal opacity-75">{beamreachAwsAccountId}</span>
                      )}
                    </div>
                    <div className="text-xs opacity-75 font-normal">Compass uses its own IAM task role — no cross-account setup needed.</div>
                  </div>
                  {awsMode === "local_iam" && (
                    <svg className="h-5 w-5 flex-shrink-0 text-white" viewBox="0 0 20 20" fill="currentColor">
                      <path fillRule="evenodd" d="M16.704 4.153a.75.75 0 0 1 .143 1.052l-8 10.5a.75.75 0 0 1-1.127.075l-4.5-4.5a.75.75 0 0 1 1.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 0 1 1.05-.143Z" clipRule="evenodd" />
                    </svg>
                  )}
                </div>
              </button>
              {awsMode === "local_iam" && (
                <button
                  type="button"
                  onClick={() => {
                    markAwsDirty();
                    setAwsMode("assume_role");
                    setShowDifferentAccount(true);
                  }}
                  className="text-xs text-gray-400 hover:text-gray-600 underline underline-offset-2 transition"
                >
                  Change target account
                </button>
              )}
              <div className="flex items-center gap-3 my-1">
                <div className="flex-1 border-t border-gray-200" />
                <span className="text-xs font-semibold text-gray-400 uppercase tracking-wide">or</span>
                <div className="flex-1 border-t border-gray-200" />
              </div>
              <div className="rounded-lg overflow-hidden">
                <button
                  type="button"
                  onClick={() => setShowDifferentAccount((prev) => !prev)}
                  className="w-full flex items-center justify-between bg-blue-600 text-white px-4 py-2 text-sm font-semibold rounded-md transition hover:bg-black"
                >
                  <span>Target a different account</span>
                  <svg
                    className={`h-4 w-4 text-white transition-transform ${showDifferentAccount ? "rotate-180" : ""}`}
                    viewBox="0 0 20 20"
                    fill="currentColor"
                  >
                    <path fillRule="evenodd" d="M5.22 8.22a.75.75 0 0 1 1.06 0L10 11.94l3.72-3.72a.75.75 0 1 1 1.06 1.06l-4.25 4.25a.75.75 0 0 1-1.06 0L5.22 9.28a.75.75 0 0 1 0-1.06Z" clipRule="evenodd" />
                  </svg>
                </button>
                {showDifferentAccount && (
                  <div className="border border-gray-200 rounded-md mt-2 px-4 py-4 space-y-4">
                    <div className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        onClick={() => {
                          markAwsDirty();
                          setAwsMode("assume_role");
                        }}
                        className={`rounded-full px-3 py-1 text-sm font-semibold transition ${
                          awsMode === "assume_role" ? "bg-gray-900 text-white" : "bg-gray-100 text-gray-700 hover:bg-gray-200"
                        }`}
                      >
                        Assume role
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          markAwsDirty();
                          setAwsMode("access_key");
                        }}
                        className={`rounded-full px-3 py-1 text-sm font-semibold transition ${
                          awsMode === "access_key" ? "bg-gray-900 text-white" : "bg-gray-100 text-gray-700 hover:bg-gray-200"
                        }`}
                      >
                        Access key fallback
                      </button>
                    </div>
                    {awsMode === "assume_role" ? (
                <div className="space-y-4">
                  <div className="rounded border border-gray-200 bg-gray-50 px-4 py-3 text-sm text-gray-700">
                    Create an IAM role in the target account that trusts the {COMPANY_NAME} account{" "}
                    <span className="font-semibold text-gray-900">{beamreachAwsAccountId}</span>. The external ID is generated by Compass below. You do not get it from AWS.
                  </div>
                  <div className="grid gap-4 sm:grid-cols-2">
                  <label className="space-y-1">
                    <span className="text-sm font-medium text-gray-700">Target account ID</span>
                    <input
                      value={awsRoleAccountId}
                      onChange={(event) => {
                        markAwsDirty();
                        setAwsRoleAccountId(event.target.value);
                      }}
                      placeholder="123456789012"
                      className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-gray-500 focus:outline-none"
                    />
                  </label>
                  <label className="space-y-1">
                    <span className="text-sm font-medium text-gray-700">Role name</span>
                    <input
                      value={awsRoleName}
                      onChange={(event) => {
                        markAwsDirty();
                        setAwsRoleName(event.target.value);
                      }}
                      placeholder="BeamreachCompassRole"
                      className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-gray-500 focus:outline-none"
                    />
                  </label>
                  </div>
                  <label className="space-y-1">
                    <span className="text-sm font-medium text-gray-700">External ID</span>
                    <input
                      value={externalIdDisplay}
                      readOnly
                      onFocus={(event) => event.target.select()}
                      className="w-full rounded border border-gray-300 bg-gray-50 px-3 py-2 text-sm text-gray-700 focus:border-gray-500 focus:outline-none"
                    />
                    <span className="text-xs text-gray-500">
                      Use this value directly if you are configuring the trust policy manually instead of using the snippets.
                    </span>
                  </label>
                  <div className="space-y-3">
                    <details className="rounded border border-gray-200 bg-gray-50 px-4 py-3">
                      <summary className="cursor-pointer text-sm font-medium text-gray-700">
                        Terraform snippet
                      </summary>
                      <div className="mt-3">
                        {externalIdForSetup ? (
                          <textarea
                            value={terraformRoleSnippet}
                            readOnly
                            className="min-h-[260px] w-full rounded border border-gray-300 bg-white px-3 py-2 font-mono text-xs text-gray-700 focus:outline-none"
                          />
                        ) : (
                          <div className="flex min-h-[120px] items-center justify-center rounded border border-gray-300 bg-white px-3 py-2 text-sm text-gray-500">
                            Generating workspace external ID...
                          </div>
                        )}
                      </div>
                    </details>
                    <details className="rounded border border-gray-200 bg-gray-50 px-4 py-3">
                      <summary className="cursor-pointer text-sm font-medium text-gray-700">
                        CloudFormation snippet
                      </summary>
                      <div className="mt-3">
                        {externalIdForSetup ? (
                          <textarea
                            value={cloudFormationRoleSnippet}
                            readOnly
                            className="min-h-[260px] w-full rounded border border-gray-300 bg-white px-3 py-2 font-mono text-xs text-gray-700 focus:outline-none"
                          />
                        ) : (
                          <div className="flex min-h-[120px] items-center justify-center rounded border border-gray-300 bg-white px-3 py-2 text-sm text-gray-500">
                            Generating workspace external ID...
                          </div>
                        )}
                      </div>
                    </details>
                  </div>
                </div>
              ) : (
                <div className="grid gap-4 sm:grid-cols-2">
                  <label className="space-y-1">
                    <span className="text-sm font-medium text-gray-700">Access key ID</span>
                    <input
                      value={awsAccessKeyId}
                      onChange={(event) => {
                        markAwsDirty();
                        setAwsAccessKeyId(event.target.value);
                      }}
                      placeholder="AKIA..."
                      className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-gray-500 focus:outline-none"
                    />
                  </label>
                  <label className="space-y-1">
                    <span className="text-sm font-medium text-gray-700">Secret access key</span>
                    <input
                      type="password"
                      value={awsSecretAccessKey}
                      onChange={(event) => {
                        markAwsDirty();
                        setAwsSecretAccessKey(event.target.value);
                      }}
                      placeholder="Paste the AWS secret access key"
                      className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-gray-500 focus:outline-none"
                    />
                  </label>
                  <label className="space-y-1 sm:col-span-2">
                    <span className="text-sm font-medium text-gray-700">Session token (only for temporary STS credentials)</span>
                    <input
                      value={awsSessionToken}
                      onChange={(event) => {
                        markAwsDirty();
                        setAwsSessionToken(event.target.value);
                      }}
                      placeholder="Leave blank for standard long-lived access keys"
                      className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-gray-500 focus:outline-none"
                    />
                  </label>
                </div>
              )}
                  </div>
                )}
              </div>
              <div className="flex items-center justify-between gap-4 flex-wrap">
                <div className="text-xs text-gray-500">
                  {settingsBootstrap?.aws?.configured
                    ? `Current target: ${settingsBootstrap.aws.account_id || "Unknown"}`
                    : "No AWS target configured yet."}
                </div>
                <button
                  type="button"
                  onClick={handleTestAwsSettings}
                  disabled={awsTesting || settingsLoading}
                  className="rounded border border-gray-300 bg-white px-4 py-2 text-sm font-semibold text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {awsTesting ? "Testing..." : "Test AWS access"}
                </button>
              </div>
            </section>}

            {githubIntegrationEnabled && <section className={`bg-white border rounded-lg shadow-sm p-6 space-y-4 ${!settingsBootstrap?.github?.connected ? "border-amber-400 ring-2 ring-amber-300" : ""}`}>
              <div className="space-y-1">
                <h2 className="text-lg font-semibold">GitHub {!settingsBootstrap?.github?.connected && <span className="ml-2 inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-800 border border-amber-300">Required</span>}</h2>
                <p className="text-sm text-gray-600">
                  {isOpenEdition ? "Pull requests go through the token in config/local.json (init_config records it; gh auth token prints yours)." : "Install the Compass GitHub App for this workspace and select the repositories Compass is allowed to access."}
                </p>
              </div>
              {githubMessage && (
                <div className="rounded border border-green-200 bg-green-50 px-3 py-2 text-sm text-green-700">
                  {githubMessage}
                </div>
              )}
              <div className="rounded border border-gray-200 bg-gray-50 px-4 py-3 text-sm text-gray-700">
                {!settingsBootstrap && settingsLoading ? (
                  <div>Checking GitHub integration status...</div>
                ) : settingsBootstrap?.github?.connected ? (
                  <div className="space-y-1">
                    <div>
                      Connected to <span className="font-semibold text-gray-900">{settingsBootstrap.github.login || "GitHub account"}</span>
                    </div>
                    {settingsBootstrap.github.repository_selection && (
                      <div className="text-xs text-gray-500">
                        Repository access: {settingsBootstrap.github.repository_selection}
                        {typeof settingsBootstrap.github.repositories_count === "number"
                          ? ` (${settingsBootstrap.github.repositories_count} repos)`
                          : ""}
                      </div>
                    )}
                    {settingsBootstrap.github.connected_at && (
                      <div className="text-xs text-gray-500">Connected at {settingsBootstrap.github.connected_at}</div>
                    )}
                    {settingsBootstrap.github.public_link && (
                      <div className="text-xs">
                        <a
                          href={settingsBootstrap.github.public_link}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-blue-600 hover:underline"
                        >
                          App settings ↗
                        </a>
                      </div>
                    )}
                  </div>
                ) : (
                  <div>GitHub is not connected. Click the button below to create and install a GitHub App for your account — Compass will guide you through the setup.</div>
                )}
              </div>
              {!isOpenEdition && <div className="flex justify-end">
                <button
                  type="button"
                  onClick={handleConnectGithub}
                  disabled={settingsLoading}
                  className="rounded bg-gray-900 px-4 py-2 text-sm font-semibold text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {settingsBootstrap?.github?.connected ? "Reinstall GitHub App" : "Connect GitHub"}
                </button>
              </div>}
            </section>}

            <SlackIntegrationCard readOnly={isReadOnly} />

            <SettingsSections slot="after_integrations" />

            <SettingsHeading view="captains" title="Captains">
              What this Compass can do, each part on or off for the workspace, with its configuration.
            </SettingsHeading>

            <CaptainsSection readOnly={isReadOnly} />

            {isOpenEdition && (
              <>
                <SettingsHeading view="updates" title="Updates">
                  The version this Compass runs and how it updates.
                </SettingsHeading>
                <CompassSection readOnly={isReadOnly} />
              </>
            )}

            <SettingsSections slot="bottom" />

            <div className="sticky bottom-0 z-10 -mx-1 border-t border-gray-200 bg-white/95 px-1 py-4 backdrop-blur">
              <div className="flex items-center justify-between gap-4 rounded-lg border border-gray-200 bg-white px-4 py-3 shadow-sm">
                <div className="min-w-0">
                  {settingsSaveSuccess ? (
                    <div className="text-sm text-green-700">{settingsSaveSuccess}</div>
                  ) : awsDirty || participantsDirty ? (
                    <div className="text-sm text-gray-700">You have unsaved settings changes.</div>
                  ) : (
                    <div className="text-sm text-gray-500">No unsaved settings changes.</div>
                  )}
                </div>
                <button
                  type="button"
                  onClick={handleSaveSettings}
                  disabled={settingsLoading || settingsSaving || awsSaving || awsTesting || participantsLoading || participantsSaving || (!awsDirty && !participantsDirty)}
                  className="shrink-0 rounded bg-gray-900 px-4 py-2 text-sm font-semibold text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {settingsSaving || awsSaving || participantsSaving ? "Saving..." : "Save settings"}
                </button>
              </div>
            </div>

          </div>
          ) : (
          <div className="rounded-lg border border-gray-200 bg-white p-6 text-sm text-gray-600 shadow-sm">
            Settings are disabled in this environment.
          </div>
          )
        ) : activeTabDef ? (
          <activeTabDef.Component />
        ) : enabledWorkspaceTabs.includes(activeTab) ? (
          // a tab the API lists but this build has no component for: a captain installed as an extension
          <GenericCaptainTab tabKey={activeTab} />
        ) : (
          <div className="rounded-lg border border-gray-200 bg-white p-6 text-sm text-gray-600 shadow-sm">
            This tab is not available in this edition.
          </div>
        )}
      </main>
    </div>
  );

  return (
    <ShellProvider value={shell}>
      {productProviders.reduceRight((children, Provider) => <Provider>{children}</Provider>, body)}
    </ShellProvider>
  );
}
