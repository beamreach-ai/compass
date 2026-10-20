"use client";
// What the dashboard shell (DashboardClient) shares with the modules the product
// plugs in through app/lib/extensions.ts. The product tabs, settings sections and
// banners read it with useShell(); the shell itself owns the state.
import React, { createContext, useContext, useEffect } from "react";
import type { useSearchParams } from "next/navigation";
import type { AppConfigSummary, DashboardTab } from "./tabs";

export type QueueEvent = {
  timestamp: number;
  message: string;
  type?: string;
  status?: string | null;
  data?: any;
};

export type QueueJob = {
  job_id: string;
  captain: string;
  command: string;
  status: string;
  queue: string;
  session_id?: string | null;
  result_summary?: any;
  error?: string | null;
  created_at: number;
  updated_at: number;
  events: QueueEvent[];
};

export type SessionSummary = {
  workspace_id: string;
  org_id?: string | null;
  user_id?: string | null;
};

export type AwsSettingsSummary = {
  configured: boolean;
  connected?: boolean;
  mode: "roles" | null;
  region?: string | null;
  regions?: string[];
  account_id?: string | null;
  account_name?: string | null;
  install_id?: string | null;
  roles?: { read?: string | null; act?: string | null };
  role_name?: string | null;
  external_id?: string | null;
  external_id_configured?: boolean;
  source_identity?: boolean;
  runtime_arn?: string | null;
  credential_source?: "access_key" | "ambient" | string | null;
  alert_queues?: Record<string, string | null>;
  state_buckets?: string[];
  connected_at?: string | null;
  verified_at?: string | null;
  verification?: Record<string, unknown> | null;
  migrated_from?: string | null;
  connect_command?: string | null;
  last_validated_at?: string | null;
  beamreach_account_id?: string | null;
  compass_account_id?: string | null;
  suggested_role_name?: string | null;
};

export type GithubSettingsSummary = {
  connected: boolean;
  mode?: string | null;
  login?: string | null;
  account_type?: string | null;
  installation_id?: number | null;
  repository_selection?: string | null;
  repositories_count?: number | null;
  connected_at?: string | null;
  app_configured: boolean;
  app_slug?: string | null;
  public_link?: string | null;
};

// The product's sections of the bootstrap (compass_api.extensions.register_bootstrap_section);
// the open API answers without them.
export type TriageSettingsSummary = {
  security_auto_triage_enabled: boolean;
  finops_auto_triage_enabled: boolean;
  finops_fixes_in_progress_limit?: number | null;
  finops_fixes_in_progress_count?: number | null;
  finops_fixes_limit_reached?: boolean;
  finops_fixes_in_progress_limit_source?: "workspace" | "task" | "unlimited" | null;
  finops_fixes_in_progress_limit_override?: number | null;
};

export type BillingSummary = {
  configured_plan?: "free" | "cloud" | "enterprise" | null;
  plan?: "free" | "cloud" | "enterprise" | null;
  cloud_monthly_price_usd?: number | null;
  usage_period_start?: string | null;
  usage_period_end?: string | null;
  audit_limit?: number | null;
  triage_limit?: number | null;
  fix_limit?: number | null;
  audits_used?: number | null;
  triages_used?: number | null;
  fixes_used?: number | null;
  audits_remaining?: number | null;
  triages_remaining?: number | null;
  fixes_remaining?: number | null;
  audit_limit_reached?: boolean;
  triage_limit_reached?: boolean;
  fix_limit_reached?: boolean;
  upgrade_required?: boolean;
  bedrock_cost_limit_usd?: number | null;
  bedrock_spend_usd?: number | null;
  bedrock_spend_remaining_usd?: number | null;
  bedrock_limit_reached?: boolean;
  enterprise_contact_email?: string | null;
  upgrade_url?: string | null;
  license_email?: string | null;
  license_activated_at?: string | null;
  portal_available?: boolean;
  subscription_grace_active?: boolean;
};

export type InfraScanSummary = {
  scanned: boolean;
  integrations_ready: boolean;
  status?: string | null;
  job_id?: string | null;
  job_status?: string | null;
  latest_message?: string | null;
  last_requested_at?: string | null;
  last_enqueued_at?: string | null;
  last_completed_at?: string | null;
  last_failed_at?: string | null;
  last_error?: string | null;
  last_trigger?: string | null;
  disabled_reason?: string | null;
};

// One row of the bootstrap's `captains`: compass_core.captains.CaptainState.to_dict()
export type CaptainSummary = {
  name: string;
  label: string;
  summary: string;
  tier: "open" | "cloud" | "enterprise";
  feature: string;
  version: string;
  requires_core: string;
  tabs: Array<{ key: string; label: string; group: string; feature: string; views: string[] }>;
  depends_on: string[];
  has_config: boolean;
  settings_component?: string | null;
  core: boolean;
  installed: boolean;
  compatible: boolean;
  licensed: boolean;
  enabled: boolean;
  blocked_by: string[];
  active: boolean;
  reason?: string | null;
  install_hint?: string | null;
  installed_from?: "page" | "env" | null;
};

// The bootstrap's `update`: compass_core.updates.UpdateState.to_dict()
export type ReleaseEntry = {
  version: string;
  published_at?: string | null;
  notes_url?: string | null;
  summary: string;
  images: Record<string, string>;
  min_from?: string | null;
  requires_restart: string[];
  notes: string[];
};

// One release from the notes feed (COMPASS_RELEASE_NOTES_URL): plain text only, never HTML.
export type ReleaseNote = {
  version: string;
  title: string;
  headline?: string | null;
  link?: string | null; // https only; the API drops anything else
  published_at?: string | null;
  summary: string;
  features: Array<{ title: string; body: string }>;
  after: string[];
};

export type UpdateState = {
  running: string;
  pinned?: string | null;
  channel: string;
  latest?: string | null;
  available: boolean;
  direct: boolean;
  release?: ReleaseEntry | null;
  checked_at?: number | null;
  error?: string | null;
  apply: "updater" | "manual" | "off";
  pinned_differs: boolean;
  via?: string | null;
  releases: ReleaseEntry[];
  // running < version <= latest, newest first; absent from an API older than 0.5.2
  release_notes?: ReleaseNote[];
  notes_source?: string | null;
  notes_error?: string | null;
};

export type SettingsBootstrap = {
  app?: AppConfigSummary;
  core_version?: string;
  update?: UpdateState | null;
  captains?: CaptainSummary[];
  workspace: SessionSummary;
  aws: AwsSettingsSummary;
  github: GithubSettingsSummary;
  triage?: TriageSettingsSummary | null;
  billing?: BillingSummary | null;
  infra_scan?: InfraScanSummary;
};

// A settings section that saves through the shell's "Save settings" bar rather
// than its own button: the bar shows its unsaved state, waits while it loads or
// saves, and calls `save` when it is dirty. When the AWS section was saved and
// this one was not dirty, `afterAwsSaved` runs instead (model availability
// depends on the AWS credentials, say).
export type SettingsSaveParticipant = {
  dirty: boolean;
  loading: boolean;
  saving: boolean;
  save: () => Promise<unknown>;
  afterAwsSaved?: () => Promise<unknown>;
};

export type Shell = {
  activeTab: DashboardTab;
  searchParams: ReturnType<typeof useSearchParams>;
  settingsBootstrap: SettingsBootstrap | null;
  settingsError: string | null;
  setSettingsBootstrap: React.Dispatch<React.SetStateAction<SettingsBootstrap | null>>;
  refreshSettingsBootstrap: () => Promise<void>;
  disabledFeatures: string[];
  infraScan: InfraScanSummary | null;
  infraScanned: boolean;
  infraScanBlocked: boolean;
  queueJobs: QueueJob[];
  queueError: string | null;
  fetchQueueJobs: () => Promise<void>;
  // the mapper job that is queued or running, if any (the infra-scan banner and the Maps tab follow it)
  activeMapperJob: QueueJob | null;
  registerSettingsSaveParticipant: (id: string, participant: SettingsSaveParticipant) => void;
  unregisterSettingsSaveParticipant: (id: string) => void;
  clearSettingsSaveSuccess: () => void;
};

const ShellContext = createContext<Shell | null>(null);

export const ShellProvider = ShellContext.Provider;

export function useShell(): Shell {
  const shell = useContext(ShellContext);
  if (!shell) {
    throw new Error("useShell() must be used inside DashboardClient");
  }
  return shell;
}

export function useSettingsSaveParticipant(id: string, participant: SettingsSaveParticipant): void {
  const { registerSettingsSaveParticipant, unregisterSettingsSaveParticipant } = useShell();
  const { dirty, loading, saving, save, afterAwsSaved } = participant;
  useEffect(() => {
    registerSettingsSaveParticipant(id, { dirty, loading, saving, save, afterAwsSaved });
  }, [id, dirty, loading, saving, save, afterAwsSaved, registerSettingsSaveParticipant]);
  useEffect(() => () => unregisterSettingsSaveParticipant(id), [id, unregisterSettingsSaveParticipant]);
}
