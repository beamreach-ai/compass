"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import HoneypotActions, { HoneypotAuditList, ResponderRolePreview } from "./HoneypotActions";
import HoneypotIncidents, { Short, type Incident, type TokenInfo } from "./HoneypotIncidents";
import { SLACK_SETTINGS_HREF, useSlackStatus } from "./SlackIntegrationCard";
import { BTN_GHOST, BTN_OUTLINE, BTN_OUTLINE_SM, BTN_QUIET_DANGER_SM, LINK_BTN, ReadOnlyContext, exactUtc, relativeTime, useReadOnly, writeProps } from "./honeypotFormat";
import { DemoTripContext, incidentHref, useDemoTrip } from "./honeypotFormat";
import type { DemoTrip, DemoTripState } from "./honeypotFormat";
import { asHoneypotView, honeypotViewHref } from "./honeypotViews";
import { isOpenEdition, PRODUCT_NAME } from "../lib/edition";

// ── Types ──────────────────────────────────────────────────────────────────

type HoneytokenClass = "aws_credential" | "db_credential" | "ssh_key" | "s3_object" | "api_token" | "dns_record";
type HoneytokenStatus = "pending_pr" | "pending_merge" | "armed" | "active" | "rotating" | "decommissioned";
type AlertIpType = "internal" | "external" | "unknown";
type Severity = "critical" | "high" | "medium" | "low";
type EndpointType = "beamreach_email" | "slack" | "pagerduty" | "webhook" | "email" | "smtp";

type MaterialSnippet = { label: string; language: string; content: string };
// UI shape. The API nests the key/value pairs under `material.material`; normalizeMaterial()
// flattens that once at the fetch boundary so components read `material.values`.
type Material = {
  values: Record<string, string>;
  secret_ref: "inline" | "terraform_output";
  planted_by: "terraform" | "user";
  instructions: string;
  snippets: MaterialSnippet[];
};

function normalizeMaterial(raw: Record<string, unknown>): Material {
  const values = (raw.values ?? raw.material ?? {}) as Record<string, string>;
  return {
    values,
    secret_ref: (raw.secret_ref as Material["secret_ref"]) ?? "inline",
    planted_by: (raw.planted_by as Material["planted_by"]) ?? "user",
    instructions: String(raw.instructions ?? ""),
    snippets: Array.isArray(raw.snippets) ? (raw.snippets as MaterialSnippet[]) : [],
  };
}

type PlacementType = "ecs_task_env" | "ecs_task_secret" | "secrets_manager" | "docker_image" | "file" | "manual";

// One placement per honeytoken, embedded on the token.
type Placement = {
  type: PlacementType;
  resource_arn: string | null;
  service_arn: string | null;
  cluster_arn: string | null;
  container: string | null;
  env_vars: string[] | null;
  tf_address: string | null;
  repo: string | null;
  pr_url: string | null;
  planted_at: number | string | null;
  verified_at: number | string | null;
  detail: string | null;
  notes: string | null;
};

type ActionConfig = {
  severity: Severity;
  notification_endpoint_ids: string[];
  runbook: string;
  auto_block_ip: boolean;
};

type Honeytoken = {
  honeytoken_id: string;
  honeytoken_class: HoneytokenClass;
  name: string;
  description: string;
  status: HoneytokenStatus;
  account_id?: string;
  region?: string;
  resource_path?: string;
  pr_url?: string;
  deployed_at?: string;
  created_at: number;
  updated_at: number;
  trigger_count: number;
  last_triggered_at?: string;
  active_alert_count: number;
  placement: Placement | null;
  action_config: ActionConfig;
  material?: Material;
};

type HoneypotAlert = {
  alert_id: string;
  honeytoken_id: string;
  triggered_at: string;
  source_ip?: string;
  ip_type?: AlertIpType;
  principal_arn?: string;
  user_agent?: string;
  service?: string;
  resolved_at?: string;
  is_test?: boolean;
};

type NotificationEndpoint = {
  endpoint_id: string;
  name: string;
  type: EndpointType;
  channel?: string;
  recipients?: string[];
  is_default?: boolean;
  bot_mode?: boolean;
  // Slack alert channel on the workspace connection (Settings → Slack): `channel` is the channel id.
  use_workspace_slack?: boolean;
  channel_name?: string;
  is_private?: boolean;
  created_at: string;
};

type MapSuggestion = {
  resource_id: string;
  resource_type: string;
  resource_name?: string;
  account_id?: string;
  region?: string;
  recommended_class: HoneytokenClass;
  reason: string;
  priority: "high" | "medium" | "low";
  default_config: Partial<ActionConfig>;
  placement?: Partial<Placement> | null;
};

// ── Constants ──────────────────────────────────────────────────────────────

const CLASS_LABELS: Record<HoneytokenClass, string> = {
  aws_credential: "AWS credential",
  db_credential: "Database credential",
  ssh_key: "SSH key",
  s3_object: "S3 object",
  api_token: "API token",
  dns_record: "DNS record",
};

const CLASS_DESCRIPTIONS: Record<HoneytokenClass, string> = {
  db_credential: "A decoy database host, user and password; a connection attempt is the alarm.",
  s3_object:  "A fake sensitive file (e.g. .env, passwords.csv) placed in an S3 bucket. Any GetObject access triggers an alert via CloudTrail.",
  aws_credential: "A fake IAM access key that should never be used. Any API call using this key is a confirmed credential theft indicator.",
  api_token:  "A fake credential string embedded in code or docs. Triggers a callback when someone tries to use it.",
  dns_record: "A DNS record that should never be queried. Alerts on any lookup — internal queries indicate lateral movement.",
  ssh_key:    "A fake SSH private key. Authentication attempts against a honey endpoint trigger an immediate critical alert.",
};

const CLASS_SEVERITY: Record<HoneytokenClass, Severity> = {
  db_credential: "critical",
  s3_object:  "high",
  aws_credential: "critical",
  api_token:  "high",
  dns_record: "medium",
  ssh_key:    "critical",
};

const CLASS_RUNBOOKS: Record<HoneytokenClass, string> = {
  db_credential:
    "1. CRITICAL: Nothing legitimate connects to this database. Any attempt is a confirmed breach indicator.\n2. Record the source IP, timestamp, and the user name attempted.\n3. Block the source IP.\n4. Find where the credentials leaked (code scan, git history, task definitions, logs).\n5. Rotate real database credentials that sit alongside this honeytoken.\n6. Open an incident and notify the security team immediately.",
  s3_object:
    "1. Record the AWS principal ARN, source IP, and timestamp.\n2. Determine if the source IP is internal or external.\n3. If external: escalate to Critical, notify security team, consider blocking IP via WAF.\n4. If internal: investigate the principal for signs of compromise, check recent activity.\n5. Rotate any real credentials that may be co-located with this honeytoken.",
  aws_credential:
    "1. CRITICAL: Any usage of this key means credentials were stolen.\n2. Immediately disable the honeytoken key.\n3. Record the service called, source IP, and requesting principal.\n4. Identify how the key may have leaked (code scan, git history, logs).\n5. Treat all co-located credentials as compromised and rotate them.\n6. Open an incident and notify the security team immediately.",
  api_token:
    "1. Record the source IP, request headers, and any payload.\n2. Determine if the caller is internal or external.\n3. Search code repos and logs for where this token may have been exposed.\n4. Rotate all real tokens of the same type.\n5. If external caller: report to threat intel team.",
  dns_record:
    "1. Record the querying IP and DNS resolver used.\n2. If internal resolver: investigate source host for lateral movement or malware.\n3. If external resolver: log as reconnaissance activity, monitor for follow-up access attempts.\n4. Check if the querying IP appears in other recent security events.",
  ssh_key:
    "1. CRITICAL: This key should never be used. Any auth attempt is a confirmed breach indicator.\n2. Record source IP, timestamp, and SSH client fingerprint.\n3. Block source IP immediately.\n4. Scan infrastructure for the origin of the key leak.\n5. Rotate all SSH keys in affected systems.\n6. Open an incident and notify the security team immediately.",
};

const STATUS_BADGE: Record<HoneytokenStatus, string> = {
  pending_pr:     "bg-yellow-100 text-yellow-700",
  pending_merge:  "bg-blue-100 text-blue-700",
  armed:          "bg-green-100 text-green-700",
  active:         "bg-green-100 text-green-700",
  rotating:       "bg-orange-100 text-orange-700",
  decommissioned: "bg-gray-100 text-gray-500",
};

const STATUS_LABELS: Record<HoneytokenStatus, string> = {
  pending_pr:     "PR open",
  pending_merge:  "Pending merge",
  armed:          "Armed",
  active:         "Active",
  rotating:       "Rotating",
  decommissioned: "Decommissioned",
};

const ENDPOINT_LABELS: Record<EndpointType, string> = {
  beamreach_email: "Email",
  slack:           "Slack",
  pagerduty:       "PagerDuty",
  webhook:         "Webhook",
  email:           "Email (own SES)",
  smtp:            "Email (own SMTP)",
};

const ENDPOINT_BADGE: Record<EndpointType, string> = {
  beamreach_email: "bg-blue-100 text-blue-700",
  slack:           "bg-purple-100 text-purple-700",
  pagerduty:       "bg-green-100 text-green-700",
  webhook:         "bg-gray-100 text-gray-700",
  email:           "bg-blue-50 text-blue-600",
  smtp:            "bg-blue-50 text-blue-600",
};

const PLACEMENT_LABELS: Record<PlacementType, string> = {
  ecs_task_env:    "ECS task env vars",
  ecs_task_secret: "ECS task secret",
  secrets_manager: "Secrets Manager",
  docker_image:    "Docker image",
  file:            "File in repo",
  manual:          "Manual",
};

const PLACEMENT_BADGE: Record<PlacementType, string> = {
  ecs_task_env:    "bg-indigo-100 text-indigo-700",
  ecs_task_secret: "bg-indigo-100 text-indigo-700",
  secrets_manager: "bg-purple-100 text-purple-700",
  docker_image:    "bg-cyan-100 text-cyan-700",
  file:            "bg-gray-100 text-gray-700",
  manual:          "bg-amber-100 text-amber-700",
};

const EMPTY_PLACEMENT: Placement = {
  type: "ecs_task_env", resource_arn: null, service_arn: null, cluster_arn: null, container: null,
  env_vars: null, tf_address: null, repo: null, pr_url: null, planted_at: null, verified_at: null,
  detail: null, notes: null,
};

function placementTime(v: number | string | null, fallback = "—"): React.ReactNode {
  if (v === null || v === "") return fallback;
  return <span title={exactUtc(v)}>{relativeTime(v)}</span>;
}

// Which fields each placement type asks for.
const PLACEMENT_FIELDS: Record<PlacementType, { key: keyof Placement; label: string; placeholder: string }[]> = {
  ecs_task_env: [
    { key: "resource_arn", label: "Task definition ARN", placeholder: "arn:aws:ecs:...:task-definition/web:3" },
    { key: "service_arn", label: "Service ARN", placeholder: "arn:aws:ecs:...:service/cluster/web" },
    { key: "cluster_arn", label: "Cluster ARN", placeholder: "arn:aws:ecs:...:cluster/prod" },
    { key: "container", label: "Container", placeholder: "web" },
    { key: "env_vars", label: "Env vars (comma separated)", placeholder: "DB_HOST, DB_USER, DB_PASSWORD" },
  ],
  ecs_task_secret: [
    { key: "resource_arn", label: "Secret ARN", placeholder: "arn:aws:secretsmanager:..." },
    { key: "service_arn", label: "Service ARN", placeholder: "arn:aws:ecs:...:service/cluster/web" },
    { key: "cluster_arn", label: "Cluster ARN", placeholder: "arn:aws:ecs:...:cluster/prod" },
    { key: "container", label: "Container", placeholder: "web" },
    { key: "env_vars", label: "Env vars (comma separated)", placeholder: "DB_PASSWORD" },
  ],
  secrets_manager: [{ key: "resource_arn", label: "Secret ARN", placeholder: "arn:aws:secretsmanager:..." }],
  docker_image: [{ key: "resource_arn", label: "Image URI", placeholder: "123456789012.dkr.ecr.us-east-1.amazonaws.com/web:latest" }],
  file: [
    { key: "repo", label: "Repository", placeholder: "owner/name" },
    { key: "resource_arn", label: "File path", placeholder: "config/.env.example" },
  ],
  manual: [],
};

const SEVERITY_BADGE: Record<Severity, string> = {
  critical: "bg-red-100 text-red-700",
  high:     "bg-orange-100 text-orange-700",
  medium:   "bg-yellow-100 text-yellow-700",
  low:      "bg-green-100 text-green-700",
};

const PRIORITY_BADGE: Record<string, string> = {
  high:   "bg-red-100 text-red-700",
  medium: "bg-yellow-100 text-yellow-700",
  low:    "bg-gray-100 text-gray-600",
};

const IP_TYPE_BADGE: Record<AlertIpType, string> = {
  external: "bg-red-100 text-red-700",
  internal: "bg-yellow-100 text-yellow-700",
  unknown:  "bg-gray-100 text-gray-600",
};

const BTN_PRIMARY   = "inline-flex items-center justify-center px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm font-semibold hover:bg-blue-700 transition disabled:opacity-50 disabled:cursor-not-allowed";
const BTN_SECONDARY = BTN_OUTLINE;
const BTN_DANGER    = "inline-flex items-center justify-center px-3 py-1.5 rounded-md bg-red-600 text-white text-sm font-semibold hover:bg-red-700 transition disabled:opacity-50 disabled:cursor-not-allowed";
const BTN_SM        = "inline-flex items-center justify-center px-2.5 py-1 rounded-md bg-blue-600 text-white text-xs font-semibold hover:bg-blue-700 transition disabled:opacity-50 disabled:cursor-not-allowed";
const BTN_SM_DANGER = "inline-flex items-center justify-center px-2.5 py-1 rounded-md bg-red-600 text-white text-xs font-semibold hover:bg-red-700 transition disabled:opacity-50 disabled:cursor-not-allowed";

function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => {
        void navigator.clipboard.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
      className="px-2 py-0.5 rounded text-xs font-medium border border-solid border-gray-200 bg-white text-gray-600 hover:bg-gray-50 transition"
    >
      {copied ? "Copied ✓" : label}
    </button>
  );
}

function HoneyPotIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M8 6h8" />
      <path d="M7 6c-2.5 2-3.5 4.5-3.5 7.5C3.5 18 6.5 20.5 12 20.5s8.5-2.5 8.5-7C20.5 10.5 19.5 8 17 6" />
      <path d="M9 11.5c1 1 2 1 3 0s2-1 3 0" />
      <path d="M12 6V3.5" />
    </svg>
  );
}

// ── Material Panel (the actual fake credential + how to plant it) ───────────────

function MaterialPanel({ material }: { material: Material }) {
  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3">
        <p className="text-sm font-semibold text-amber-900">How to plant this honeytoken</p>
        <p className="text-xs text-amber-800 mt-1">{material.instructions}</p>
        {material.secret_ref === "terraform_output" && (
          <p className="text-xs text-amber-700 mt-2">
            The real secret is minted by Terraform and available as a sensitive output once the PR is applied.
          </p>
        )}
      </div>

      {Object.keys(material.values).length > 0 && (
        <div className="space-y-1.5">
          <p className="text-xs font-semibold text-gray-700 uppercase tracking-wide">Material</p>
          {Object.entries(material.values).map(([k, v]) => (
            <div key={k} className="flex items-center justify-between gap-3 rounded border border-gray-200 bg-white px-3 py-1.5">
              <div className="min-w-0">
                <span className="text-xs text-gray-400">{k}</span>
                <div className="font-mono text-xs text-gray-800 truncate">{String(v)}</div>
              </div>
              <CopyButton text={String(v)} />
            </div>
          ))}
        </div>
      )}

      {material.snippets.map((snip) => (
        <div key={snip.label} className="space-y-1">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-gray-700">{snip.label}</span>
            <CopyButton text={snip.content} />
          </div>
          <pre className="rounded bg-gray-900 text-gray-100 text-xs p-3 overflow-x-auto whitespace-pre-wrap">{snip.content}</pre>
        </div>
      ))}
    </div>
  );
}

// ── Modal frame (create/edit honeytoken) ───────────────────────────────────

// Escape and a click on the backdrop close it.
function useEscape(onClose: () => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
}

const CLOSE_X = "bg-transparent hover:bg-transparent px-2 py-0 text-gray-400 hover:text-gray-600 text-xl leading-none";

// ── Notification endpoints (inline section on the Settings view) ────────────

type SlackChannel = { id: string; name: string; is_private: boolean; is_member: boolean };

/** Slack channels of the connected workspace; loaded only while connected. */
function useSlackChannels(connected: boolean): { channels: SlackChannel[] | null; error: string | null } {
  const [channels, setChannels] = useState<SlackChannel[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!connected) { setChannels(null); return; }
    let cancelled = false;
    setError(null);
    fetch("/api/integrations/slack/channels")
      .then(async (res) => {
        const d = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
        return (d.channels ?? []) as SlackChannel[];
      })
      .then((list) => { if (!cancelled) setChannels(list); })
      .catch((err) => { if (!cancelled) { setChannels([]); setError(err instanceof Error ? err.message : "Could not load Slack channels"); } });
    return () => { cancelled = true; };
  }, [connected]);
  return { channels, error };
}

function EndpointsPanel({
  endpoints,
  onRefresh,
}: {
  endpoints: NotificationEndpoint[];
  onRefresh: () => void;
}) {
  const readOnly = useReadOnly();
  const [type, setType] = useState<EndpointType>(isOpenEdition ? "webhook" : "beamreach_email");
  const [name, setName] = useState("");
  const [webhookUrl, setWebhookUrl] = useState("");
  const [recipients, setRecipients] = useState("");
  const [routingKey, setRoutingKey] = useState("");
  const [smtpHost, setSmtpHost] = useState("");
  const [smtpPort, setSmtpPort] = useState("");
  const [smtpSecurity, setSmtpSecurity] = useState<"starttls" | "ssl" | "none">("starttls");
  const [smtpUser, setSmtpUser] = useState("");
  const [smtpPass, setSmtpPass] = useState("");
  const [smtpFrom, setSmtpFrom] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, string>>({});
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [slackStatus] = useSlackStatus();
  const slackConnected = slackStatus?.connected === true;
  const { channels: slackChannels, error: slackChannelsError } = useSlackChannels(slackConnected);
  const [slackChannelId, setSlackChannelId] = useState("");
  const [slackAdding, setSlackAdding] = useState(false);
  const [slackError, setSlackError] = useState<string | null>(null);
  const usedSlackChannels = new Set(endpoints.filter((e) => e.use_workspace_slack).map((e) => e.channel));
  const pickableSlackChannels = (slackChannels ?? []).filter((c) => !usedSlackChannels.has(c.id));
  const pickedSlackChannel = pickableSlackChannels.find((c) => c.id === slackChannelId) ?? null;

  const handleAddSlackChannel = async () => {
    if (!pickedSlackChannel) { setSlackError("Pick a channel"); return; }
    setSlackAdding(true);
    setSlackError(null);
    try {
      const res = await fetch("/api/integrations/slack/endpoints", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel: pickedSlackChannel.id, channel_name: pickedSlackChannel.name, is_private: pickedSlackChannel.is_private }),
      });
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || `HTTP ${res.status}`); }
      setSlackChannelId("");
      onRefresh();
    } catch (err) {
      setSlackError(err instanceof Error ? err.message : "Failed to add the Slack channel");
    } finally {
      setSlackAdding(false);
    }
  };

  const handleAdd = async () => {
    if (!name.trim()) { setError("Name is required"); return; }
    if (type === "webhook" && !webhookUrl.trim()) { setError("Webhook URL is required"); return; }
    if (type === "slack" && !webhookUrl.trim()) { setError("Webhook URL is required"); return; }
    if (type === "smtp" && (!smtpHost.trim() || !smtpFrom.trim())) { setError("SMTP host and from address are required"); return; }
    if ((type === "email" || type === "beamreach_email" || type === "smtp") && !recipients.trim()) { setError("At least one recipient is required"); return; }
    if (type === "pagerduty" && !routingKey.trim()) { setError("Routing key is required"); return; }
    setSaving(true);
    setError(null);
    try {
      const body: Record<string, unknown> = { name: name.trim(), type };
      if (type === "slack") { body.webhook_url = webhookUrl.trim(); }
      if (type === "smtp") {
        body.host = smtpHost.trim();
        body.port = smtpPort.trim() ? Number(smtpPort) : undefined;
        body.use_tls = smtpSecurity === "starttls";
        body.use_ssl = smtpSecurity === "ssl";
        body.username = smtpUser.trim() || undefined;
        body.password = smtpUser.trim() ? smtpPass : undefined;
        body.from_address = smtpFrom.trim();
      }
      if (type === "webhook") { body.webhook_url = webhookUrl.trim(); }
      if (type === "pagerduty") { body.routing_key = routingKey.trim(); }
      if (type === "email" || type === "beamreach_email" || type === "smtp") { body.recipients = recipients.split(",").map((r) => r.trim()).filter(Boolean); }
      const res = await fetch("/api/notifications/endpoints", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || `HTTP ${res.status}`); }
      setName(""); setWebhookUrl(""); setRecipients(""); setRoutingKey(""); setSmtpHost(""); setSmtpPort(""); setSmtpUser(""); setSmtpPass(""); setSmtpFrom(""); onRefresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add endpoint");
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async (ep: NotificationEndpoint) => {
    const id = ep.endpoint_id;
    setTestingId(id);
    setTestResult((prev) => ({ ...prev, [id]: "testing…" }));
    try {
      const url = ep.use_workspace_slack ? `/api/integrations/slack/endpoints/${id}/test` : `/api/notifications/endpoints/${id}/test`;
      const res = await fetch(url, { method: "POST" });
      const d = await res.json().catch(() => ({}));
      setTestResult((prev) => ({ ...prev, [id]: res.ok ? "sent" : (d.detail || "failed") }));
    } catch {
      setTestResult((prev) => ({ ...prev, [id]: "error" }));
    } finally {
      setTestingId(null);
    }
  };

  const handleDelete = async (id: string) => {
    if (!window.confirm("Remove this notification endpoint?")) return;
    setDeletingId(id);
    try {
      await fetch(`/api/notifications/endpoints/${id}`, { method: "DELETE" });
      onRefresh();
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <section aria-label="Notifications" className="rounded-lg border border-gray-200 bg-white px-4 py-3 space-y-4">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">Notifications</h2>
          <p className="text-sm text-gray-600">Where alerts go when a honeytoken trips. Each honeytoken picks from these endpoints.</p>
        </div>

        <div aria-label="Slack channel" role="group" className="space-y-2">
          <p className="text-sm font-medium text-gray-700">Slack channel</p>
          {slackStatus === null ? (
            <p className="text-xs text-gray-400">Checking Slack…</p>
          ) : slackConnected ? (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <select
                  aria-label="Slack channel to add"
                  className="min-w-0 flex-1 border border-gray-200 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
                  value={slackChannelId}
                  onChange={(e) => { setSlackChannelId(e.target.value); setSlackError(null); }}
                  disabled={slackChannels === null || readOnly}
                >
                  <option value="">{slackChannels === null ? "Loading channels…" : pickableSlackChannels.length ? "Pick a channel" : "No more channels to add"}</option>
                  {pickableSlackChannels.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.is_private ? "🔒 " : "#"}{c.name}{c.is_private && !c.is_member ? ` — invite @${PRODUCT_NAME} first` : ""}
                    </option>
                  ))}
                </select>
                <button
                  onClick={() => void handleAddSlackChannel()}
                  {...writeProps(readOnly, slackAdding || !pickedSlackChannel)}
                  className={BTN_PRIMARY}
                >
                  {slackAdding ? "Adding…" : "Add"}
                </button>
              </div>
              {pickedSlackChannel?.is_private && !pickedSlackChannel.is_member && (
                <p className="text-xs text-amber-700">Private channel: invite @{PRODUCT_NAME} first (<code>/invite @{PRODUCT_NAME}</code> in the channel).</p>
              )}
              {(slackError || slackChannelsError) && <p className="text-sm text-red-500">{slackError || slackChannelsError}</p>}
              <p className="text-xs text-gray-400">
                Posted by the Slack workspace connected in Settings{slackStatus?.team_name ? ` (${slackStatus.team_name})` : ""}; one thread per incident.
              </p>
            </>
          ) : (
            <p className="text-xs text-gray-500">
              Slack isn&apos;t connected.{" "}
              <Link href={SLACK_SETTINGS_HREF} className="text-blue-600 hover:underline">Connect it in Settings →</Link>
            </p>
          )}
        </div>

        {endpoints.length > 0 && (
          <ul className="divide-y divide-gray-100 rounded border border-gray-200">
            {endpoints.map((ep) => (
              <li key={ep.endpoint_id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2.5 text-sm">
                <div className="min-w-0">
                  <span className="font-medium text-gray-900">{ep.name}</span>
                  <span className={`ml-2 px-1.5 py-0.5 rounded text-xs font-medium ${ENDPOINT_BADGE[ep.type] ?? "bg-gray-100 text-gray-600"}`}>
                    {ENDPOINT_LABELS[ep.type] ?? ep.type}
                  </span>
                  {ep.is_default && <span className="ml-1 text-xs text-green-600">created for you</span>}
                  {ep.use_workspace_slack ? (
                    <span className="ml-1 text-gray-500 text-xs">
                      {ep.is_private ? "🔒 " : "#"}{ep.channel_name || ep.channel}
                    </span>
                  ) : (
                    <>
                      {ep.bot_mode && <span className="ml-1 text-xs text-gray-400">threads</span>}
                      {ep.channel && <span className="ml-1 text-gray-400 text-xs">#{ep.channel}</span>}
                    </>
                  )}
                  {ep.recipients && ep.recipients.length > 0 && (
                    <span className="ml-1 text-gray-400 text-xs">{ep.recipients.join(", ")}</span>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  {testResult[ep.endpoint_id] && (
                    <span className={`text-xs ${testResult[ep.endpoint_id] === "sent" ? "text-green-600" : "text-red-500"}`}>
                      {testResult[ep.endpoint_id]}
                    </span>
                  )}
                  <button
                    onClick={() => void handleTest(ep)}
                    {...writeProps(readOnly, testingId === ep.endpoint_id)}
                    className={BTN_SM}
                  >
                    Test
                  </button>
                  <button
                    onClick={() => void handleDelete(ep.endpoint_id)}
                    {...writeProps(readOnly, deletingId === ep.endpoint_id)}
                    className={BTN_SM_DANGER}
                  >
                    Remove
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}

        <div className="space-y-3 border-t border-gray-100 pt-4">
          <p className="text-sm font-medium text-gray-700">Add endpoint</p>
          <div className="flex flex-wrap gap-2">
            {((isOpenEdition ? ["slack", "pagerduty", "webhook", "email", "smtp"] : ["beamreach_email", "slack", "pagerduty", "webhook", "email", "smtp"]) as EndpointType[]).map((t) => (
              <button
                key={t}
                onClick={() => setType(t)}
                className={`px-3 py-2 rounded border text-sm font-medium transition ${type === t ? "border-blue-400 bg-blue-50 text-blue-700" : "border-gray-200 bg-white text-gray-600 hover:bg-gray-50"}`}
              >
                {ENDPOINT_LABELS[t]}
                {t === "beamreach_email" && <span className="ml-1 text-xs text-green-600">easiest</span>}
              </button>
            ))}
          </div>
          <input
            className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
            placeholder="Endpoint name (e.g. Security Team)"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          {type === "slack" && (
            <>
              <input
                className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
                placeholder="Slack incoming webhook URL"
                value={webhookUrl}
                onChange={(e) => setWebhookUrl(e.target.value)}
              />
              <p className="text-xs text-gray-400">
                Advanced: a Slack incoming webhook (alerts only, no threads). Prefer the Slack channel picker above, which uses the workspace connection from Settings.
              </p>
            </>
          )}
          {type === "beamreach_email" && (
            <>
              <input
                className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
                placeholder="Recipient emails, comma-separated"
                value={recipients}
                onChange={(e) => setRecipients(e.target.value)}
              />
              <p className="text-xs text-gray-400">
                Sent by Beamreach on your behalf — no AWS SES setup and no license required.
              </p>
            </>
          )}
          {type === "email" && (
            <>
              <input
                className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
                placeholder="Recipient emails, comma-separated"
                value={recipients}
                onChange={(e) => setRecipients(e.target.value)}
              />
              <p className="text-xs text-gray-400">
                Advanced: sends from your own account&apos;s SES (configure a verified sender in Settings). Use this to keep alert delivery inside your account.
              </p>
            </>
          )}
          {type === "smtp" && (
            <>
              <input className={INPUT_CLS} placeholder="SMTP host" value={smtpHost} onChange={(e) => setSmtpHost(e.target.value)} />
              <div className="flex gap-2">
                <input className={INPUT_CLS} placeholder="Port (default by security)" value={smtpPort} onChange={(e) => setSmtpPort(e.target.value)} />
                <select className={INPUT_CLS} value={smtpSecurity} onChange={(e) => setSmtpSecurity(e.target.value as "starttls" | "ssl" | "none")}>
                  <option value="starttls">STARTTLS</option>
                  <option value="ssl">SSL/TLS</option>
                  <option value="none">None</option>
                </select>
              </div>
              <input className={INPUT_CLS} placeholder="Username (optional)" value={smtpUser} onChange={(e) => setSmtpUser(e.target.value)} />
              <input className={INPUT_CLS} placeholder="Password" type="password" value={smtpPass} onChange={(e) => setSmtpPass(e.target.value)} />
              <input className={INPUT_CLS} placeholder="From address" value={smtpFrom} onChange={(e) => setSmtpFrom(e.target.value)} />
              <input className={INPUT_CLS} placeholder="Recipient emails, comma-separated" value={recipients} onChange={(e) => setRecipients(e.target.value)} />
              <p className="text-xs text-gray-400">
                Sends through your own mail server; credentials are stored server-side and never shown again.
              </p>
            </>
          )}
          {type === "webhook" && (
            <>
              <input
                className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
                placeholder="Webhook URL (receives a JSON POST)"
                value={webhookUrl}
                onChange={(e) => setWebhookUrl(e.target.value)}
              />
              <p className="text-xs text-gray-400">
                We POST {"{ subject, text, severity }"} as JSON on each alert.
              </p>
            </>
          )}
          {type === "pagerduty" && (
            <>
              <input
                className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
                placeholder="PagerDuty integration/routing key"
                value={routingKey}
                onChange={(e) => setRoutingKey(e.target.value)}
              />
              <p className="text-xs text-gray-400">
                From a PagerDuty service → Integrations → Events API v2. Critical honeytokens can page on-call.
              </p>
            </>
          )}
          {error && <p className="text-sm text-red-500">{error}</p>}
          <button
            onClick={() => void handleAdd()}
            {...writeProps(readOnly, saving)}
            className={`w-full ${BTN_PRIMARY}`}
          >
            {saving ? "Adding…" : "Add endpoint"}
          </button>
        </div>
    </section>
  );
}

// ── Placement form + card (one placement per honeytoken) ─────────────────────

const INPUT_CLS = "w-full border border-gray-200 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400";

function placementToBody(p: Placement): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  (Object.keys(EMPTY_PLACEMENT) as (keyof Placement)[]).forEach((k) => {
    const v = p[k];
    out[k] = typeof v === "string" ? (v.trim() || null) : v;
  });
  return out;
}

function PlacementForm({ value, onChange }: { value: Placement; onChange: (p: Placement) => void }) {
  const set = (key: keyof Placement, raw: string) => {
    if (key === "env_vars") {
      onChange({ ...value, env_vars: raw.split(",").map((x) => x.trim()).filter(Boolean) });
    } else {
      onChange({ ...value, [key]: raw } as Placement);
    }
  };
  const [envText, setEnvText] = useState((value.env_vars ?? []).join(", "));
  return (
    <div className="space-y-2">
      <div>
        <label htmlFor="ht-placement-type" className="block text-xs font-medium text-gray-700 mb-1">Placement type</label>
        <select
                id="ht-placement-type"
          className={INPUT_CLS}
          value={value.type}
          onChange={(e) => onChange({ ...value, type: e.target.value as PlacementType })}
        >
          {(Object.keys(PLACEMENT_LABELS) as PlacementType[]).map((t) => (
            <option key={t} value={t}>{PLACEMENT_LABELS[t]}</option>
          ))}
        </select>
      </div>
      {PLACEMENT_FIELDS[value.type].map((f) => (
        <div key={f.key}>
          <label className="block text-xs font-medium text-gray-700 mb-1">{f.label}</label>
          <input
            className={INPUT_CLS}
            placeholder={f.placeholder}
            value={f.key === "env_vars" ? envText : String(value[f.key] ?? "")}
            onChange={(e) => {
              if (f.key === "env_vars") setEnvText(e.target.value);
              set(f.key, e.target.value);
            }}
          />
        </div>
      ))}
      {value.type === "manual" && (
        <>
          <p className="text-xs text-amber-700">
            Manual placements get no automatic correlation: an alert cannot point at a task, container or env var.
          </p>
          <div>
            <label htmlFor="ht-detail" className="block text-xs font-medium text-gray-700 mb-1">Detail</label>
            <input
                id="ht-detail"
              className={INPUT_CLS}
              placeholder="e.g. Confluence: payments runbook"
              value={value.detail ?? ""}
              onChange={(e) => set("detail", e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="ht-notes" className="block text-xs font-medium text-gray-700 mb-1">Notes</label>
            <input
                id="ht-notes" className={INPUT_CLS} value={value.notes ?? ""} onChange={(e) => set("notes", e.target.value)} />
          </div>
        </>
      )}
    </div>
  );
}

function PlacementCard({ honeytoken, onChanged }: { honeytoken: Honeytoken; onChanged: () => void }) {
  const readOnly = useReadOnly();
  const [placement, setPlacement] = useState<Placement | null>(honeytoken.placement);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Placement>(honeytoken.placement ?? EMPTY_PLACEMENT);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { setPlacement(honeytoken.placement); }, [honeytoken.placement]);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/api/honeypots/tokens/${honeytoken.honeytoken_id}/placement`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(placementToBody(draft)),
      });
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail ? JSON.stringify(d.detail) : `HTTP ${res.status}`); }
      const d = await res.json();
      setPlacement(d.placement ?? null);
      setEditing(false);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save placement");
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    setBusy(true);
    try {
      const res = await fetch(`/api/honeypots/tokens/${honeytoken.honeytoken_id}/placement`, { method: "DELETE" });
      if (res.ok) { setPlacement(null); setDraft(EMPTY_PLACEMENT); onChanged(); }
    } finally {
      setBusy(false);
    }
  };

  const prUrl = placement?.pr_url || honeytoken.pr_url || null;
  const row = (label: string, value: React.ReactNode) => (
    <div className="flex gap-2 text-xs">
      <span className="w-24 shrink-0 text-gray-400">{label}</span>
      <span className="min-w-0 break-all text-gray-700">{value}</span>
    </div>
  );

  return (
    <div className="rounded-lg border border-gray-200 bg-white px-4 py-3 space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs font-semibold text-gray-700 uppercase tracking-wide">
          Placement <span className="text-gray-400 normal-case">— where this honeytoken lives (one per honeytoken)</span>
        </p>
        {!editing && (
          <div className="flex gap-2">
            <button
              onClick={() => { setDraft(placement ?? EMPTY_PLACEMENT); setEditing(true); }}
              {...writeProps(readOnly)}
              className={BTN_SM}
            >
              {placement ? "Edit" : "Set placement"}
            </button>
            {placement && <button onClick={() => void clear()} {...writeProps(readOnly, busy)} className={BTN_SM_DANGER}>Clear</button>}
          </div>
        )}
      </div>

      {editing ? (
        <div className="space-y-2">
          <PlacementForm value={draft} onChange={setDraft} />
          {error && <p className="text-xs text-red-500">{error}</p>}
          <div className="flex justify-end gap-2">
            <button onClick={() => setEditing(false)} className={BTN_SM}>Cancel</button>
            <button onClick={() => void save()} disabled={busy} className={BTN_SM}>{busy ? "Saving…" : "Save placement"}</button>
          </div>
        </div>
      ) : !placement ? (
        <p className="text-xs text-gray-400">
          No placement recorded. Saying where the honeytoken is lets alerts pinpoint the breach source.
        </p>
      ) : (
        <div className="space-y-1">
          <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${PLACEMENT_BADGE[placement.type]}`}>
            {PLACEMENT_LABELS[placement.type]}
          </span>
          {placement.type === "manual" && (
            <p className="text-xs text-amber-700">Manual placement: no automatic correlation to a resource.</p>
          )}
          {placement.resource_arn && row("Resource", placement.resource_arn)}
          {placement.service_arn && row("Service", placement.service_arn)}
          {placement.cluster_arn && row("Cluster", placement.cluster_arn)}
          {placement.container && row("Container", placement.container)}
          {placement.env_vars && placement.env_vars.length > 0 && row("Env vars", placement.env_vars.join(", "))}
          {placement.tf_address && row("Terraform", <span className="font-mono">{placement.tf_address}</span>)}
          {placement.repo && row("Repo", placement.repo)}
          {placement.detail && row("Detail", placement.detail)}
          {placement.notes && row("Notes", placement.notes)}
          {prUrl && row("PR", (
            <a href={prUrl} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">{prUrl} ↗</a>
          ))}
          {row("Planted", placementTime(placement.planted_at, prUrl ? "PR open" : "—"))}
          {row("Verified", placementTime(placement.verified_at))}
        </div>
      )}
    </div>
  );
}

// ── Honeytoken Create/Edit Modal ────────────────────────────────────────────────

function HoneytokenModal({
  honeytoken,
  endpoints,
  onClose,
  onSaved,
}: {
  honeytoken?: Honeytoken | null;
  endpoints: NotificationEndpoint[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const isEdit = Boolean(honeytoken);
  const [step, setStep] = useState<1 | 2 | 3>(isEdit ? 2 : 1);
  const [honeytokenClass, setHoneytokenClass] = useState<HoneytokenClass>(honeytoken?.honeytoken_class ?? "s3_object");
  const [name, setName] = useState(honeytoken?.name ?? "");
  const [description, setDescription] = useState(honeytoken?.description ?? "");
  const [accountId, setAccountId] = useState(honeytoken?.account_id ?? "");
  const [region, setRegion] = useState(honeytoken?.region ?? "");
  const [resourcePath, setResourcePath] = useState(honeytoken?.resource_path ?? "");
  const [severity, setSeverity] = useState<Severity>(honeytoken?.action_config.severity ?? CLASS_SEVERITY[honeytokenClass]);
  const [runbook, setRunbook] = useState(honeytoken?.action_config.runbook ?? CLASS_RUNBOOKS[honeytokenClass]);
  const [selectedEndpoints, setSelectedEndpoints] = useState<string[]>(honeytoken?.action_config.notification_endpoint_ids ?? []);
  const [autoBlock, setAutoBlock] = useState(honeytoken?.action_config.auto_block_ip ?? false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createdMaterial, setCreatedMaterial] = useState<Material | null>(null);
  const [withPlacement, setWithPlacement] = useState(false);
  const [placementDraft, setPlacementDraft] = useState<Placement>(EMPTY_PLACEMENT);
  useEscape(onClose);

  const handleClassSelect = (cls: HoneytokenClass) => {
    setHoneytokenClass(cls);
    if (!isEdit) {
      setSeverity(CLASS_SEVERITY[cls]);
      setRunbook(CLASS_RUNBOOKS[cls]);
    }
  };

  const toggleEndpoint = (id: string) => {
    setSelectedEndpoints((prev) => prev.includes(id) ? prev.filter((e) => e !== id) : [...prev, id]);
  };

  const handleSave = async () => {
    if (!name.trim()) { setError("Name is required"); return; }
    setSaving(true);
    setError(null);
    const body = {
      honeytoken_class: honeytokenClass,
      name: name.trim(),
      description: description.trim(),
      account_id: accountId.trim() || undefined,
      region: region.trim() || undefined,
      resource_path: resourcePath.trim() || undefined,
      action_config: {
        severity,
        runbook,
        notification_endpoint_ids: selectedEndpoints,
        auto_block_ip: autoBlock,
      },
      ...(!isEdit && withPlacement ? { placement: placementToBody(placementDraft) } : {}),
    };
    try {
      const url = isEdit ? `/api/honeypots/tokens/${honeytoken!.honeytoken_id}` : "/api/honeypots/tokens";
      const method = isEdit ? "PUT" : "POST";
      const res = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || `HTTP ${res.status}`); }
      const saved = await res.json().catch(() => ({}));
      onSaved();
      // On create, keep the modal open to reveal the material and planting steps.
      if (!isEdit && saved && saved.material) {
        setCreatedMaterial(normalizeMaterial(saved.material as Record<string, unknown>));
      } else {
        onClose();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save honeytoken");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={isEdit ? "Edit honeytoken" : "New honeytoken"}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="bg-white rounded-xl shadow-xl w-full max-w-2xl p-4 sm:p-6 space-y-5 max-h-[calc(100dvh-2rem)] overflow-y-auto">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-900">
            {createdMaterial ? "Honeytoken ready — plant it" : isEdit ? "Edit honeytoken" : "New honeytoken"}
          </h2>
          <button onClick={onClose} aria-label="Close" className={CLOSE_X}>&times;</button>
        </div>

        {createdMaterial && (
          <div className="space-y-4">
            <div className="rounded bg-green-50 border border-green-200 text-green-800 px-3 py-2 text-sm">
              Honeytoken created and a plant PR is being generated. Plant the material below, then confirm where you put it in the
              <span className="font-medium"> Placement</span> card so alerts can pinpoint the breach source.
            </div>
            <MaterialPanel material={createdMaterial} />
            <div className="flex justify-end">
              <button onClick={onClose} className={BTN_PRIMARY}>Done</button>
            </div>
          </div>
        )}

        {!isEdit && !createdMaterial && (
          <div className="flex gap-2 text-xs font-medium">
            {([1, 2, 3] as const).map((s) => (
              <span key={s} className={`px-2 py-1 rounded ${step === s ? "bg-blue-600 text-white" : "bg-gray-100 text-gray-500"}`}>
                {s === 1 ? "Honeytoken type" : s === 2 ? "Location" : "Action"}
              </span>
            ))}
          </div>
        )}

        {!createdMaterial && step === 1 && (
          <div className="grid grid-cols-1 gap-3">
            {(Object.keys(CLASS_LABELS) as HoneytokenClass[]).map((cls) => (
              <button
                key={cls}
                onClick={() => handleClassSelect(cls)}
                aria-pressed={honeytokenClass === cls}
                className={`text-left rounded-lg border border-solid p-3 transition ${honeytokenClass === cls ? "border-blue-500 bg-blue-50 hover:bg-blue-50 ring-1 ring-blue-500" : "border-gray-200 bg-white hover:border-gray-300 hover:bg-gray-50"}`}
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium text-gray-900 text-sm">{CLASS_LABELS[cls]}</span>
                  <span className={`px-2 py-0.5 rounded text-xs font-medium ${SEVERITY_BADGE[CLASS_SEVERITY[cls]]}`}>
                    {CLASS_SEVERITY[cls]}
                  </span>
                </div>
                <p className="text-xs text-gray-600 mt-1 font-normal">{CLASS_DESCRIPTIONS[cls]}</p>
              </button>
            ))}
            <div className="flex justify-end pt-2">
              <button onClick={() => setStep(2)} className={BTN_PRIMARY}>
                Next →
              </button>
            </div>
          </div>
        )}

        {!createdMaterial && step === 2 && (
          <div className="space-y-3">
            {!isEdit && (
              <div className="flex items-center gap-2 p-2 rounded bg-blue-50 border border-blue-200 text-sm text-blue-800">
                <span className="font-medium">{CLASS_LABELS[honeytokenClass]}</span>
                <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${SEVERITY_BADGE[CLASS_SEVERITY[honeytokenClass]]}`}>
                  {CLASS_SEVERITY[honeytokenClass]}
                </span>
              </div>
            )}
            <div>
              <label htmlFor="ht-name" className="block text-xs font-medium text-gray-700 mb-1">Name *</label>
              <input
                id="ht-name"
                className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
                placeholder={`e.g. prod-backup-reporting`}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div>
              <label htmlFor="ht-description" className="block text-xs font-medium text-gray-700 mb-1">Description</label>
              <input
                id="ht-description"
                className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
                placeholder="What is this honeytoken for?"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label htmlFor="ht-aws-account-id" className="block text-xs font-medium text-gray-700 mb-1">AWS Account ID</label>
                <input
                id="ht-aws-account-id"
                  className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
                  placeholder="123456789012"
                  value={accountId}
                  onChange={(e) => setAccountId(e.target.value)}
                />
              </div>
              <div>
                <label htmlFor="ht-region" className="block text-xs font-medium text-gray-700 mb-1">Region</label>
                <input
                id="ht-region"
                  className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
                  placeholder="us-east-1"
                  value={region}
                  onChange={(e) => setRegion(e.target.value)}
                />
              </div>
            </div>
            <div>
              <label htmlFor="ht-resource-path" className="block text-xs font-medium text-gray-700 mb-1">Resource path</label>
              <input
                id="ht-resource-path"
                className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
                placeholder="e.g. my-bucket/secrets/passwords.csv"
                value={resourcePath}
                onChange={(e) => setResourcePath(e.target.value)}
              />
            </div>
            {!isEdit && (
              <div className="rounded-lg border border-gray-200 px-3 py-3 space-y-2">
                <label className="flex items-center gap-2 cursor-pointer">
                  <input type="checkbox" checked={withPlacement} onChange={(e) => setWithPlacement(e.target.checked)} className="rounded border-gray-300" />
                  <span className="text-sm font-medium text-gray-700">Set the placement now</span>
                  <span className="text-xs text-gray-400">(where this honeytoken will live; one per honeytoken)</span>
                </label>
                {withPlacement && <PlacementForm value={placementDraft} onChange={setPlacementDraft} />}
              </div>
            )}
            <div className="flex justify-between pt-2">
              {!isEdit && (
                <button onClick={() => setStep(1)} className={BTN_SECONDARY}>
                  ← Back
                </button>
              )}
              <button onClick={() => isEdit ? void handleSave() : setStep(3)} className={`ml-auto ${BTN_PRIMARY}`}>
                {isEdit ? (saving ? "Saving…" : "Save changes") : "Next →"}
              </button>
            </div>
          </div>
        )}

        {!createdMaterial && step === 3 && (
          <div className="space-y-4">
            <div>
              <label className="block text-xs font-medium text-gray-700 mb-1">Severity</label>
              <div className="flex flex-wrap gap-2">
                {(["critical", "high", "medium", "low"] as Severity[]).map((s) => (
                  <button
                    key={s}
                    onClick={() => setSeverity(s)}
                    className={`px-3 py-1.5 rounded text-xs font-medium border border-solid transition ${severity === s ? SEVERITY_BADGE[s] + " border-current" : "border-gray-200 bg-white text-gray-500 hover:bg-gray-50"}`}
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <label className="block text-xs font-medium text-gray-700 mb-1">Notify via</label>
              {endpoints.length === 0 ? (
                <p className="text-xs text-gray-400">No endpoints configured. Add one under Settings → Notifications.</p>
              ) : (
                <div className="flex flex-wrap gap-2">
                  {endpoints.map((ep) => (
                    <button
                      key={ep.endpoint_id}
                      onClick={() => toggleEndpoint(ep.endpoint_id)}
                      className={`px-3 py-1.5 rounded text-xs font-medium border border-solid transition ${selectedEndpoints.includes(ep.endpoint_id) ? "border-blue-400 bg-blue-50 text-blue-700" : "border-gray-200 bg-white text-gray-600 hover:bg-gray-50"}`}
                    >
                      {ep.name} ({ep.type})
                    </button>
                  ))}
                </div>
              )}
            </div>

            <div>
              <label htmlFor="ht-action-runbook" className="block text-xs font-medium text-gray-700 mb-1">Action runbook</label>
              <textarea
                id="ht-action-runbook"
                className="w-full border border-gray-200 rounded px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-blue-400"
                rows={8}
                value={runbook}
                onChange={(e) => setRunbook(e.target.value)}
              />
              <p className="text-xs text-gray-400 mt-1">Pre-populated from honeytoken class defaults. Customize as needed.</p>
            </div>

            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={autoBlock}
                onChange={(e) => setAutoBlock(e.target.checked)}
                className="rounded border-gray-300"
              />
              <span className="text-sm text-gray-700">Auto-block source IP when triggered (via WAF/Security Group)</span>
            </label>

            {error && <p className="text-sm text-red-500">{error}</p>}

            <div className="flex justify-between pt-2">
              <button onClick={() => setStep(2)} className={BTN_SECONDARY}>
                ← Back
              </button>
              <button
                onClick={() => void handleSave()}
                disabled={saving}
                className={BTN_PRIMARY}
              >
                {saving ? "Creating…" : "Create honeytoken & open PR"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Alert + Action Panel (expanded row) ────────────────────────────────────

function AlertPanel({
  honeytoken,
  endpoints,
  onClose,
  onHoneytokenChanged,
}: {
  honeytoken: Honeytoken;
  endpoints: NotificationEndpoint[];
  onClose: () => void;
  onHoneytokenChanged: () => void;
}) {
  const readOnly = useReadOnly();
  const [alerts, setAlerts] = useState<HoneypotAlert[]>([]);
  const [loading, setLoading] = useState(true);
  const [runbook, setRunbook] = useState(honeytoken.action_config.runbook);
  const [notifyIds, setNotifyIds] = useState<string[]>(honeytoken.action_config.notification_endpoint_ids);
  const [executing, setExecuting] = useState(false);
  const [executeResult, setExecuteResult] = useState<string | null>(null);
  const [resolvingId, setResolvingId] = useState<string | null>(null);

  const [material, setMaterial] = useState<Material | null>(null);
  const [materialOpen, setMaterialOpen] = useState(false);

  const [testFiring, setTestFiring] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);

  // demo mode: "Trip it" on a pre-armed decoy, with the trip in force (shared by every visitor)
  const demoTrip = useDemoTrip();
  const trippable = demoTrip.demo && demoTrip.trippable.includes(honeytoken.honeytoken_id);
  const currentTrip = demoTrip.cooldowns[honeytoken.honeytoken_id];
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!trippable) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [trippable]);
  const secondsLeft = currentTrip ? Math.max(0, Math.ceil((currentTrip.until - now) / 1000)) : 0;
  const tripInForce = Boolean(currentTrip) && secondsLeft > 0;
  const [tripping, setTripping] = useState(false);
  const [tripResult, setTripResult] = useState<React.ReactNode>(null);
  const handleTrip = async () => {
    setTripping(true);
    setTripResult(null);
    try {
      const res = await fetch(`/api/honeypots/tokens/${honeytoken.honeytoken_id}/trip`, { method: "POST" });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) setTripResult(typeof d.detail === "string" ? d.detail : "The trip was refused");
      else if (d.state === "cooldown")
        setTripResult(<>Tripped {relativeTime(d.tripped_at)} by {d.by}; {d.incident_id ? <>the incident is open: <Link href={incidentHref(d.incident_id)} className={LINK_BTN}>open it</Link></> : "CloudTrail is still delivering the event; the incident opens below"}.</>);
      else setTripResult("Tripped. CloudTrail takes one to five minutes to deliver the event; then the incident opens below and the response runs.");
      demoTrip.refresh();
      setTimeout(loadAlerts, 800);
    } catch {
      setTripResult("The trip failed");
    } finally {
      setTripping(false);
    }
  };

  const loadAlerts = useCallback(() => {
    fetch(`/api/honeypots/tokens/${honeytoken.honeytoken_id}/alerts`)
      .then((r) => r.json())
      .then((d) => { setAlerts(Array.isArray(d) ? d : d.alerts ?? []); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [honeytoken.honeytoken_id]);

  useEffect(() => {
    loadAlerts();
  }, [loadAlerts]);

  const handleViewMaterial = async () => {
    if (!materialOpen && !material) {
      try {
        const res = await fetch(`/api/honeypots/tokens/${honeytoken.honeytoken_id}/material`);
        const d = await res.json().catch(() => ({}));
        if (res.ok && d.material) setMaterial(normalizeMaterial(d.material as Record<string, unknown>));
      } catch {}
    }
    setMaterialOpen((v) => !v);
  };

  const handleArm = async () => {
    setTestResult(null);
    const res = await fetch(`/api/honeypots/tokens/${honeytoken.honeytoken_id}/arm`, { method: "POST" });
    if (res.ok) {
      onHoneytokenChanged();
      return;
    }
    const d = await res.json().catch(() => ({}));
    const checks = (d.detail?.checks ?? []) as { name: string; ok: boolean; detail: string }[];
    const failed = checks.filter((c) => !c.ok).map((c) => `${c.name}: ${c.detail}`);
    setTestResult(`Not armed. ${failed.length ? failed.join("; ") : (typeof d.detail === "string" ? d.detail : "Verification failed")}`);
  };

  const handleTestFire = async () => {
    setTestFiring(true);
    setTestResult(null);
    try {
      const res = await fetch(`/api/honeypots/tokens/${honeytoken.honeytoken_id}/test-fire`, { method: "POST" });
      const d = await res.json().catch(() => ({}));
      let msg: string;
      if (!res.ok) msg = typeof d.detail === "string" ? d.detail : "Test fire failed";
      else if (d.status === "not_delivered") msg = `Not delivered: ${d.note ?? "no alert arrived"}`;
      else if (d.mode === "real") msg = `Test alert fired and delivered in ${d.round_trip_ms} ms.`;
      else msg = `Test alert fired — check your notification endpoints. ${d.note ?? ""}`.trim();
      setTestResult(msg);
      if (res.ok) setTimeout(loadAlerts, 800);
    } catch {
      setTestResult("Test fire failed");
    } finally {
      setTestFiring(false);
    }
  };

  const toggleNotify = (id: string) => {
    setNotifyIds((prev) => prev.includes(id) ? prev.filter((e) => e !== id) : [...prev, id]);
  };

  const handleExecute = async () => {
    setExecuting(true);
    setExecuteResult(null);
    try {
      const res = await fetch(`/api/honeypots/tokens/${honeytoken.honeytoken_id}/execute-runbook`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ runbook, notification_endpoint_ids: notifyIds }),
      });
      const d = await res.json().catch(() => ({}));
      setExecuteResult(res.ok ? "Runbook dispatched — notifications sent." : (d.detail || "Failed to execute runbook"));
    } catch {
      setExecuteResult("Failed to execute runbook");
    } finally {
      setExecuting(false);
    }
  };

  const handleResolve = async (alertId: string) => {
    setResolvingId(alertId);
    try {
      await fetch(`/api/honeypots/tokens/${honeytoken.honeytoken_id}/alerts/${alertId}/resolve`, { method: "POST" });
      setAlerts((prev) => prev.map((a) => a.alert_id === alertId ? { ...a, resolved_at: new Date().toISOString() } : a));
    } finally {
      setResolvingId(null);
    }
  };

  const activeAlerts = alerts.filter((a) => !a.resolved_at);
  const resolvedAlerts = alerts.filter((a) => a.resolved_at);

  return (
    <div className="border-t border-rose-200 bg-rose-50/60 px-4 py-4 space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-gray-800">Alert details</span>
          {activeAlerts.length > 0 && (
            <span className="px-2 py-0.5 rounded-full bg-rose-200 text-rose-800 text-xs font-semibold">
              {activeAlerts.length} active
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button onClick={() => void handleViewMaterial()} className={BTN_SM}>
            {materialOpen ? "Hide material" : "View material"}
          </button>
          {(honeytoken.status === "pending_pr" || honeytoken.status === "pending_merge") && (
            <button onClick={() => void handleArm()} {...writeProps(readOnly)} className={BTN_SM}>
              Verify and arm
            </button>
          )}
          {!demoTrip.demo && (
            <button onClick={() => void handleTestFire()} {...writeProps(readOnly, testFiring)} className={BTN_SM}>
              {testFiring ? "Firing…" : "Test fire"}
            </button>
          )}
          {trippable && (
            <button
              data-trip-it
              onClick={() => void handleTrip()}
              {...writeProps(readOnly, tripping || tripInForce || honeytoken.status !== "armed")}
              title={tripInForce ? `Tripped by ${currentTrip?.by}; the decoy re-arms after the response` : honeytoken.status !== "armed" ? "Re-arming after the last trip" : "Use this decoy's key for real: the response you see is the real one"}
              className={`${BTN_SM} ${tripInForce ? "" : "ring-2 ring-amber-300"}`}
            >
              {tripping ? "Tripping…" : tripInForce ? `Tripped · ${Math.floor(secondsLeft / 60)}:${String(secondsLeft % 60).padStart(2, "0")} left` : honeytoken.status !== "armed" ? "Re-arming…" : "Trip it"}
            </button>
          )}
          <button onClick={onClose} className="bg-transparent hover:bg-transparent px-2 py-0 text-gray-500 hover:text-gray-800 text-sm">Collapse ↑</button>
        </div>
      </div>

      {testResult && (
        <p className={`text-xs ${testResult.includes("fired") && !testResult.startsWith("Not") ? "text-green-600" : "text-red-500"}`}>{testResult}</p>
      )}
      {trippable && (tripResult || (currentTrip && tripInForce)) && (
        <p data-trip-state className="text-xs text-amber-800">
          {tripResult ?? (
            <>Tripped {relativeTime(currentTrip!.tripped_at)} by {currentTrip!.by}; {currentTrip!.incident_id ? <>the incident is open: <Link href={incidentHref(currentTrip!.incident_id)} className={LINK_BTN}>open it</Link></> : "CloudTrail is delivering the event; the incident opens below"}.</>
          )}
        </p>
      )}

      {materialOpen && (
        material ? (
          <div className="rounded-lg border border-amber-200 bg-white px-4 py-3">
            <MaterialPanel material={material} />
          </div>
        ) : (
          <p className="text-xs text-gray-400">Loading material…</p>
        )
      )}

      <PlacementCard honeytoken={honeytoken} onChanged={onHoneytokenChanged} />

      {loading ? (
        <p className="text-sm text-gray-400">Loading alerts…</p>
      ) : alerts.length === 0 ? (
        <p className="text-sm text-gray-500">No alerts recorded yet for this honeytoken.</p>
      ) : (
        <div className="space-y-2">
          {activeAlerts.map((alert) => (
            <div key={alert.alert_id} className="flex items-start justify-between rounded-lg border border-rose-200 bg-white px-3 py-2.5 text-xs gap-4">
              <div className="space-y-0.5 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-medium text-gray-800" title={exactUtc(alert.triggered_at)}>{relativeTime(alert.triggered_at)}</span>
                  {alert.is_test && (
                    <span className="px-1.5 py-0.5 rounded text-xs font-medium bg-gray-200 text-gray-600">TEST</span>
                  )}
                  {alert.ip_type && (
                    <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${IP_TYPE_BADGE[alert.ip_type]}`}>
                      {alert.ip_type} IP
                    </span>
                  )}
                  <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${SEVERITY_BADGE[honeytoken.action_config.severity]}`}>
                    {honeytoken.action_config.severity}
                  </span>
                </div>
                {alert.source_ip && <div className="text-gray-500">Source IP: <span className="font-mono text-gray-800">{alert.source_ip}</span></div>}
                {alert.principal_arn && <div className="text-gray-500 truncate">Principal: <Short value={alert.principal_arn} className="text-gray-800" /></div>}
                {alert.service && <div className="text-gray-500">Service: <span className="text-gray-800">{alert.service}</span></div>}
                {alert.user_agent && <div className="text-gray-500 truncate">UA: <span className="text-gray-800">{alert.user_agent}</span></div>}
              </div>
              <button
                onClick={() => void handleResolve(alert.alert_id)}
                {...writeProps(readOnly, resolvingId === alert.alert_id)}
                className={`shrink-0 ${BTN_SM}`}
              >
                {resolvingId === alert.alert_id ? "…" : "Resolve"}
              </button>
            </div>
          ))}
          {resolvedAlerts.length > 0 && (
            <p className="text-xs text-gray-400">{resolvedAlerts.length} resolved alert{resolvedAlerts.length > 1 ? "s" : ""} hidden.</p>
          )}
        </div>
      )}

      <div className="space-y-3 border-t border-rose-200 pt-4">
        <p className="text-xs font-semibold text-gray-700 uppercase tracking-wide">Execute runbook</p>
        <div className="flex flex-wrap gap-2">
          {endpoints.map((ep) => (
            <button
              key={ep.endpoint_id}
              onClick={() => toggleNotify(ep.endpoint_id)}
              className={`px-2.5 py-1 rounded text-xs font-medium border border-solid transition ${notifyIds.includes(ep.endpoint_id) ? "border-blue-400 bg-blue-50 text-blue-700" : "border-gray-200 bg-white text-gray-500 hover:bg-gray-50"}`}
            >
              {ep.name}
            </button>
          ))}
          {endpoints.length === 0 && <p className="text-xs text-gray-400">No notification endpoints configured.</p>}
        </div>
        <textarea
          className="w-full border border-gray-200 rounded px-3 py-2 text-xs font-mono bg-white focus:outline-none focus:ring-1 focus:ring-blue-400"
          rows={6}
          value={runbook}
          onChange={(e) => setRunbook(e.target.value)}
        />
        {executeResult && (
          <p className={`text-xs ${executeResult.includes("dispatched") ? "text-green-600" : "text-red-500"}`}>
            {executeResult}
          </p>
        )}
        <button
          onClick={() => void handleExecute()}
          {...writeProps(readOnly, executing)}
          className={BTN_DANGER}
        >
          {executing ? "Sending…" : "Execute & notify"}
        </button>
      </div>
    </div>
  );
}

// ── Map Suggestions Panel ──────────────────────────────────────────────────

function SuggestionsPanel({
  endpoints,
  onPlanted,
  onClose,
}: {
  endpoints: NotificationEndpoint[];
  onPlanted: () => void;
  onClose: () => void;
}) {
  const readOnly = useReadOnly();
  const [suggestions, setSuggestions] = useState<MapSuggestion[]>([]);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [plantingId, setPlantingId] = useState<string | null>(null);
  const [planted, setPlanted] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [autopilotResult, setAutopilotResult] = useState<string | null>(null);

  const fetchSuggestions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/honeypots/suggestions");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setSuggestions(Array.isArray(data) ? data : data.suggestions ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load suggestions");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void fetchSuggestions(); }, [fetchSuggestions]);

  const handlePlant = async (suggestion: MapSuggestion) => {
    setPlantingId(suggestion.resource_id);
    try {
      const body = {
        honeytoken_class: suggestion.recommended_class,
        name: "",
        description: suggestion.reason,
        account_id: suggestion.account_id,
        region: suggestion.region,
        resource_path: suggestion.resource_id,
        action_config: {
          severity: suggestion.default_config.severity ?? CLASS_SEVERITY[suggestion.recommended_class],
          runbook: suggestion.default_config.runbook ?? CLASS_RUNBOOKS[suggestion.recommended_class],
          notification_endpoint_ids: endpoints.map((e) => e.endpoint_id),
          auto_block_ip: false,
        },
        ...(suggestion.placement ? { placement: { ...EMPTY_PLACEMENT, ...suggestion.placement } } : {}),
      };
      const res = await fetch("/api/honeypots/tokens", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setPlanted((prev) => new Set([...prev, suggestion.resource_id]));
      onPlanted();
    } catch {
    } finally {
      setPlantingId(null);
    }
  };

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const selectableIds = suggestions.filter((s) => !planted.has(s.resource_id)).map((s) => s.resource_id);
  const allSelected = selectableIds.length > 0 && selectableIds.every((id) => selected.has(id));

  const toggleSelectAll = () => {
    setSelected(allSelected ? new Set() : new Set(selectableIds));
  };

  const plantSelected = async () => {
    const chosen = suggestions.filter((s) => selected.has(s.resource_id) && !planted.has(s.resource_id));
    if (chosen.length === 0) return;
    setSubmitting(true);
    setAutopilotResult(null);
    try {
      const res = await fetch("/api/honeypots/autopilot", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          suggestions: chosen.map((s) => ({
            resource_id: s.resource_id,
            recommended_class: s.recommended_class,
            resource_name: s.resource_name,
            account_id: s.account_id,
            region: s.region,
            description: s.reason,
            default_config: s.default_config,
          })),
          notification_endpoint_ids: endpoints.map((e) => e.endpoint_id),
        }),
      });
      const d = await res.json().catch(() => ({}));
      if (res.ok) {
        setAutopilotResult(`${d.created ?? chosen.length} honeypot${chosen.length > 1 ? "s" : ""} planted — PRs are being generated.`);
        setPlanted((prev) => new Set([...prev, ...chosen.map((s) => s.resource_id)]));
        setSelected(new Set());
        onPlanted();
      } else {
        setAutopilotResult(d.detail || "Failed to plant selected");
      }
    } catch {
      setAutopilotResult("Failed to plant selected");
    } finally {
      setSubmitting(false);
    }
  };

  const selectedCount = selectableIds.filter((id) => selected.has(id)).length;

  return (
    <div className="rounded-lg border border-blue-200 bg-blue-50 p-4 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-sm font-semibold text-blue-800">Suggested honeypot locations</span>
          {!loading && suggestions.length > 0 && (
            <span className="text-xs text-blue-600">{suggestions.length} found from infra map</span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:gap-3">
          {selectableIds.length > 0 && (
            <button onClick={toggleSelectAll} {...writeProps(readOnly)} className={BTN_SM}>
              {allSelected ? "Clear" : "Select all"}
            </button>
          )}
          <button
            onClick={() => void plantSelected()}
            {...writeProps(readOnly, submitting || selectedCount === 0)}
            className={BTN_PRIMARY}
          >
            {submitting ? "Planting…" : `Plant selected${selectedCount > 0 ? ` (${selectedCount})` : ""}`}
          </button>
          <button onClick={() => void fetchSuggestions()} className={BTN_SM}>Refresh</button>
          <button onClick={onClose} className={BTN_SM}>↑ Collapse</button>
        </div>
      </div>

      <p className="text-xs text-blue-600">
        Review the suggestions, tick the ones you want, and plant them in one batch — each opens a PR you approve before anything deploys.
      </p>

      {autopilotResult && (
        <p className={`text-xs ${autopilotResult.includes("planted") ? "text-green-700" : "text-red-600"}`}>{autopilotResult}</p>
      )}

      {error && <p className="text-xs text-red-500">{error}</p>}

      {loading ? (
        <p className="text-sm text-blue-600">Analyzing infra map…</p>
      ) : suggestions.length === 0 ? (
        <p className="text-sm text-blue-700">No suggestions found. Run an infra scan first to populate the map.</p>
      ) : (
        <div className="space-y-2">
          {suggestions.map((s) => {
            const isPlanted = planted.has(s.resource_id);
            return (
              <div key={s.resource_id} className={`flex items-center justify-between rounded-lg border bg-white px-3 py-2.5 gap-4 ${isPlanted ? "opacity-60" : ""}`}>
                <div className="flex items-start gap-3 min-w-0">
                  {!isPlanted && (
                    <input
                      type="checkbox"
                      checked={selected.has(s.resource_id)}
                      onChange={() => toggleSelect(s.resource_id)}
                      className="mt-1 rounded border-gray-300"
                    />
                  )}
                  <div className="min-w-0 space-y-0.5">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium text-gray-900 truncate">{s.resource_name ?? s.resource_id}</span>
                    <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${PRIORITY_BADGE[s.priority]}`}>{s.priority}</span>
                    <span className="px-1.5 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-600">{CLASS_LABELS[s.recommended_class]}</span>
                  </div>
                  <p className="text-xs text-gray-500">{s.reason}</p>
                  {s.region && <p className="text-xs text-gray-400">{s.account_id} · {s.region}</p>}
                  </div>
                </div>
                <button
                  onClick={() => void handlePlant(s)}
                  {...writeProps(readOnly, isPlanted || plantingId === s.resource_id)}
                  className={`shrink-0 ${BTN_SM}`}
                >
                  {isPlanted ? "Planted" : plantingId === s.resource_id ? "Planting…" : "Plant"}
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Default target-repo selector ───────────────────────────────────────────

type RepoCandidate = { repo: string; module_count: number };

function RepoSelector() {
  const readOnly = useReadOnly();
  const [repos, setRepos] = useState<RepoCandidate[]>([]);
  const [defaultRepo, setDefaultRepo] = useState("");
  const [autoDefault, setAutoDefault] = useState("");
  const [manual, setManual] = useState("");
  const [manualMode, setManualMode] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/honeypots/repos");
      if (!res.ok) return;
      const d = await res.json();
      setRepos(Array.isArray(d.repos) ? d.repos : []);
      setDefaultRepo(d.default_repo ?? "");
      setAutoDefault(d.auto_default ?? "");
    } catch {}
  }, []);

  useEffect(() => { void load(); }, [load]);

  const save = async (repo: string) => {
    setSaving(true);
    setSaved(false);
    try {
      const res = await fetch("/api/honeypots/repo-default", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo }),
      });
      if (res.ok) {
        setDefaultRepo(repo);
        setSaved(true);
        setTimeout(() => setSaved(false), 1500);
        void load();
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-lg border border-gray-200 bg-white px-4 py-3 flex items-center gap-3 flex-wrap">
      <div className="min-w-0">
        <p className="text-xs font-semibold text-gray-700">Default honeypot repo</p>
        <p className="text-xs text-gray-400">
          Where PRs land when a resource has no owning repo in the infra map. Leave on Auto to use the most-used repo discovered in the map.
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-2 sm:ml-auto">
        {!manualMode ? (
          <>
            <select
              value={defaultRepo}
              onChange={(e) => void save(e.target.value)}
              {...writeProps(readOnly, saving)}
              className="border border-gray-200 rounded px-2 py-1.5 text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-400 min-w-[200px] max-w-full"
            >
              <option value="">Auto (from map){autoDefault ? ` — ${autoDefault}` : ""}</option>
              {repos.map((r) => (
                <option key={r.repo} value={r.repo}>
                  {r.repo}{r.module_count > 0 ? ` (${r.module_count} tf module${r.module_count > 1 ? "s" : ""})` : ""}
                </option>
              ))}
              {/* Show a currently-saved repo even if it's not in the discovered list */}
              {defaultRepo && !repos.some((r) => r.repo === defaultRepo) && (
                <option value={defaultRepo}>{defaultRepo}</option>
              )}
            </select>
            <button onClick={() => { setManual(defaultRepo); setManualMode(true); }} {...writeProps(readOnly)} className={`text-xs ${LINK_BTN} disabled:text-gray-400 disabled:no-underline`}>
              custom…
            </button>
          </>
        ) : (
          <>
            <input
              className="border border-gray-200 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400 min-w-[240px]"
              placeholder="owner/name"
              value={manual}
              onChange={(e) => setManual(e.target.value)}
            />
            <button onClick={() => { void save(manual.trim()); setManualMode(false); }} {...writeProps(readOnly, saving)} className={BTN_SM}>
              Save
            </button>
            <button onClick={() => setManualMode(false)} className="text-xs bg-transparent hover:bg-transparent p-0 text-gray-500 hover:underline">cancel</button>
          </>
        )}
        {saved && <span className="text-xs text-green-600">Saved ✓</span>}
      </div>
    </div>
  );
}

// ── Autopilot switch (Overview header) ──────────────────────────────────────

function AutopilotSwitch() {
  const readOnly = useReadOnly();
  const [on, setOn] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The initial read must not overwrite a toggle that raced ahead of it (a
  // slow GET, or StrictMode's second effect run in dev).
  const touched = useRef(false);
  useEffect(() => {
    let cancelled = false;
    fetch("/api/honeypots/settings")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (!cancelled && !touched.current) setOn(Boolean(d?.autopilot)); })
      .catch(() => { if (!cancelled && !touched.current) setOn(false); });
    return () => { cancelled = true; };
  }, []);

  // Optimistic: flip at once, revert with the error if the API refuses.
  const toggle = async () => {
    if (on === null) return;
    touched.current = true;
    const next = !on;
    setOn(next);
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/honeypots/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ autopilot: next }),
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof d.detail === "string" ? d.detail : `HTTP ${res.status}`);
      setOn(Boolean(d.autopilot ?? next));
    } catch (err) {
      setOn(!next);
      setError(err instanceof Error ? err.message : "Could not save");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex w-full items-start gap-3 rounded-lg border border-gray-200 bg-white px-4 py-3 sm:w-auto sm:max-w-sm">
      <div className="min-w-0 flex-1">
        <span id="honeypot-autopilot-label" className="text-sm font-semibold text-gray-900">Autopilot</span>
        <p className="text-xs text-gray-500">Plants every honeytoken the infra map suggests, with default settings.</p>
        {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
      </div>
      <button
        role="switch"
        aria-checked={on === true}
        aria-labelledby="honeypot-autopilot-label"
        onClick={() => void toggle()}
        {...writeProps(readOnly, busy || on === null)}
        // Inline so neither the global button colours nor :disabled repaint the track.
        style={{ backgroundColor: on ? "#2563eb" : "#d1d5db" }}
        className="relative mt-0.5 inline-flex h-6 w-11 shrink-0 items-center rounded-full p-0 transition disabled:cursor-not-allowed disabled:opacity-50"
      >
        <span className={`inline-block h-5 w-5 rounded-full bg-white shadow transition-transform ${on ? "translate-x-5" : "translate-x-0.5"}`} />
      </button>
    </div>
  );
}

// ── Main Tab ───────────────────────────────────────────────────────────────

const VIEW_LINK = "text-sm font-medium text-blue-600 hover:underline";

export default function HoneypotsTab() {
  const view = asHoneypotView(useSearchParams().get("view"));
  const [readOnly, setReadOnly] = useState(false);
  const [honeytokens, setHoneytokens] = useState<Honeytoken[]>([]);
  const [endpoints, setEndpoints] = useState<NotificationEndpoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [editHoneytoken, setEditHoneytoken] = useState<Honeytoken | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [showSuggestions, setShowSuggestions] = useState(false);

  const tokenInfo = useCallback(
    (id: string): TokenInfo | null => {
      const t = honeytokens.find((h) => h.honeytoken_id === id);
      return t ? { name: t.name, classLabel: CLASS_LABELS[t.honeytoken_class] ?? t.honeytoken_class } : null;
    },
    [honeytokens],
  );

  // Status strip numbers, all derived from what is already loaded.
  const armedCount = honeytokens.filter((h) => h.status === "armed").length;
  const openCount = incidents.filter((i) => i.status === "open").length;
  const ts = (v: unknown) => Date.parse(String(v ?? ""));
  const lastTrip = incidents
    .filter((i) => Number.isFinite(ts(i.first_event_time)))
    .sort((a, b) => ts(b.first_event_time) - ts(a.first_event_time))[0];
  const lastContained = incidents
    .filter((i) => Number.isFinite(ts(i.action_verified_at)))
    .sort((a, b) => ts(b.action_verified_at) - ts(a.action_verified_at))[0];
  const containSecs = lastContained
    ? Math.round((ts(lastContained.action_verified_at) - ts(lastContained.first_event_time)) / 1000)
    : NaN;
  const tripName = lastTrip ? (lastTrip.honeytoken_ids || []).map((id) => tokenInfo(id)?.name ?? id.slice(0, 8)).join(", ") : "";

  const fetchHoneytokens = useCallback(async () => {
    try {
      const res = await fetch("/api/honeypots/tokens");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setHoneytokens(Array.isArray(data) ? data : data.honeytokens ?? []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load honeytokens");
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchEndpoints = useCallback(async () => {
    try {
      const res = await fetch("/api/notifications/endpoints");
      if (!res.ok) return;
      const data = await res.json();
      setEndpoints(Array.isArray(data) ? data : data.endpoints ?? []);
    } catch {}
  }, []);

  useEffect(() => {
    void fetchHoneytokens();
    void fetchEndpoints();
    const id = setInterval(() => void fetchHoneytokens(), 30_000);
    return () => clearInterval(id);
  }, [fetchHoneytokens, fetchEndpoints]);

  // Read-only mode (demo stand): writes come back 403, so the controls are disabled.
  useEffect(() => {
    fetch("/api/settings/bootstrap")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        setReadOnly(d?.app?.read_only === true);
        setDemo(d?.app?.mode === "demo");
      })
      .catch(() => {});
  }, []);

  // Demo mode: which decoys "Trip it" fires and the trip in force on each (docs/demo.md)
  const [demo, setDemo] = useState(false);
  const [demoTrip, setDemoTrip] = useState<DemoTripState>({ demo: false, trippable: [], cooldownSeconds: 0, cooldowns: {}, refresh: () => undefined });
  const refreshDemoTrip = useCallback(() => {
    fetch("/api/demo")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!d) return;
        const now = Date.now();
        const cooldowns: Record<string, DemoTrip> = {};
        for (const row of (d.cooldowns ?? []) as Array<Record<string, any>>) {
          const id = String(row.honeytoken_id ?? "");
          if (id) cooldowns[id] = { ...row, honeytoken_id: id, in_force: row.in_force === true, by: String(row.by ?? "a visitor"), until: now + Number(row.seconds_left ?? 0) * 1000 } as DemoTrip;
        }
        setDemoTrip({ demo: true, trippable: Array.isArray(d.trippable) ? d.trippable.map(String) : [], cooldownSeconds: Number(d.cooldown_seconds ?? 0), cooldowns, refresh: refreshDemoTrip });
      })
      .catch(() => {});
  }, []);
  useEffect(() => {
    if (!demo) return;
    refreshDemoTrip();
    const id = setInterval(refreshDemoTrip, 10_000);
    return () => clearInterval(id);
  }, [demo, refreshDemoTrip]);

  const handleDelete = async (honeytoken: Honeytoken) => {
    if (!window.confirm(`Decommission honeytoken "${honeytoken.name}"? This will also generate a PR to remove the honeytoken from your infrastructure.`)) return;
    setDeletingId(honeytoken.honeytoken_id);
    try {
      await fetch(`/api/honeypots/tokens/${honeytoken.honeytoken_id}`, { method: "DELETE" });
      void fetchHoneytokens();
    } finally {
      setDeletingId(null);
    }
  };

  const Tile = ({ label, value, sub, tone }: { label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: string }) => (
    <div className="rounded-lg border border-gray-200 bg-white px-4 py-3 min-w-0">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`mt-1 text-2xl font-semibold leading-tight ${tone ?? "text-gray-900"}`}>{value}</div>
      <div className="mt-0.5 truncate text-xs text-gray-500">{sub ?? " "}</div>
    </div>
  );

  return (
    <ReadOnlyContext.Provider value={readOnly}>
    <DemoTripContext.Provider value={demoTrip}>
    <div className="space-y-6">
      {/* Header: the same on every view; the right side depends on the view. */}
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
            <HoneyPotIcon />
            Honeypots
          </h1>
          <p className="text-sm text-gray-500 mt-0.5">Honeytokens: decoy credentials and objects with no legitimate use. The moment one is used, you know.</p>
        </div>
        {view === "overview" && <AutopilotSwitch />}
        {view === "honeytokens" && (
          <div className="flex items-center gap-2 flex-wrap">
            <button
              onClick={() => { void fetchHoneytokens(); void fetchEndpoints(); }}
              title="Refresh"
              aria-label="Refresh"
              className={BTN_GHOST}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M21 12a9 9 0 1 1-2.64-6.36" />
                <path d="M21 3v6h-6" />
              </svg>
            </button>
            <button onClick={() => setShowSuggestions((v) => !v)} className={BTN_OUTLINE}>
              Map suggestions
            </button>
            <button onClick={() => setShowCreateModal(true)} {...writeProps(readOnly)} className={BTN_PRIMARY}>
              + New honeytoken
            </button>
          </div>
        )}
      </div>

      {/* Error */}
      {error && (
        <div className="rounded border border-red-200 bg-red-50 text-red-700 px-4 py-2 text-sm">{error}</div>
      )}

      {/* ── Overview: strip, latest incident, links ── */}
      {view === "overview" && (
        <>
          {!loading && honeytokens.length === 0 ? (
            <div className="rounded-lg border border-dashed border-gray-200 bg-gray-50 px-6 py-12 text-center">
              <p className="text-sm text-gray-700 font-medium">No honeytokens yet.</p>
              <p className="text-xs text-gray-500 mt-1">
                Plant the first one under{" "}
                <Link href={honeypotViewHref("honeytokens")} className={VIEW_LINK}>Honeytokens →</Link>
                {" "}or switch on Autopilot.
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <Tile label="Armed honeytokens" value={armedCount} sub={`of ${honeytokens.length} total`} />
              <Tile
                label="Open incidents"
                value={openCount}
                tone={openCount > 0 ? "text-red-700" : undefined}
                sub={openCount > 0 ? "needs attention" : "all contained or closed"}
              />
              <Tile
                label="Last trip"
                value={lastTrip ? <span title={exactUtc(String(lastTrip.first_event_time))}>{relativeTime(String(lastTrip.first_event_time))}</span> : "—"}
                sub={tripName}
              />
              <Tile
                label="Last containment"
                value={lastContained ? <span title={exactUtc(String(lastContained.action_verified_at))}>{relativeTime(String(lastContained.action_verified_at))}</span> : "—"}
                sub={Number.isFinite(containSecs) ? `in ${containSecs} s` : ""}
              />
            </div>
          )}

          <HoneypotIncidents show="latest" tokenInfo={tokenInfo} onLoaded={setIncidents} />

          <div className="flex flex-wrap gap-x-6 gap-y-2">
            <Link href={honeypotViewHref("history")} className={VIEW_LINK}>All incidents →</Link>
            <Link href={honeypotViewHref("honeytokens")} className={VIEW_LINK}>Honeytokens →</Link>
          </div>
        </>
      )}

      {/* ── Honeytokens: suggestions, the table, the modals ── */}
      {view === "honeytokens" && (
        <>
          {showSuggestions && (
            <SuggestionsPanel endpoints={endpoints} onPlanted={fetchHoneytokens} onClose={() => setShowSuggestions(false)} />
          )}

          <section>
            <h2 className="text-lg font-semibold text-gray-900">
              Honeytokens {!loading && <span className="text-sm font-normal text-gray-500">({honeytokens.length})</span>}
            </h2>
            <div className="mt-3">
          {loading ? (
            <div className="text-sm text-gray-500 py-8 text-center">Loading…</div>
          ) : honeytokens.length === 0 ? (
            <div className="rounded-lg border border-dashed border-gray-200 bg-gray-50 px-6 py-12 text-center">
              <p className="text-sm text-gray-700 font-medium">No honeypot honeytokens yet.</p>
              <p className="text-xs text-gray-500 mt-1">Create one manually or use Map suggestions to auto-detect good locations.</p>
            </div>
          ) : (
            <div className="rounded-lg border border-gray-200 bg-white overflow-x-auto">
              <table className="rt m-0 w-full text-sm shadow-none">
                <thead>
                  <tr className="bg-gray-50 border-b border-gray-200 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
                    <th className="px-4 py-3">Honeytoken</th>
                    <th className="px-4 py-3">Class</th>
                    <th className="px-4 py-3">Location</th>
                    <th className="px-4 py-3">Placement</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3">Last triggered</th>
                    <th className="px-4 py-3 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {honeytokens.map((honeytoken) => {
                    const hasAlert = honeytoken.active_alert_count > 0;
                    const isExpanded = expandedId === honeytoken.honeytoken_id;
                    return (
                      <React.Fragment key={honeytoken.honeytoken_id}>
                        <tr
                          className={`cursor-pointer transition-colors ${hasAlert ? "bg-rose-50 hover:bg-rose-100/70" : "hover:bg-gray-50"}`}
                          onClick={() => setExpandedId(isExpanded ? null : honeytoken.honeytoken_id)}
                        >
                          <td className="rt-block px-4 py-3">
                            <div className="flex items-center gap-2">
                              {hasAlert && (
                                <span className="w-2 h-2 rounded-full bg-rose-400 shrink-0 animate-pulse" title="Active alerts" />
                              )}
                              <div className="min-w-0">
                                <div className="font-medium text-gray-900">{honeytoken.name}</div>
                                {honeytoken.description && (
                                  <div className="text-xs text-gray-400 truncate max-w-xs">{honeytoken.description}</div>
                                )}
                              </div>
                            </div>
                          </td>
                          <td data-label="Class" className="px-4 py-3">
                            <div className="flex flex-wrap items-center justify-end gap-1.5 md:justify-start">
                              <span className="text-xs text-gray-600">{CLASS_LABELS[honeytoken.honeytoken_class]}</span>
                              <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${SEVERITY_BADGE[honeytoken.action_config.severity]}`}>
                                {honeytoken.action_config.severity}
                              </span>
                            </div>
                          </td>
                          <td data-label="Location" className="px-4 py-3 text-xs text-gray-500">
                            <div className="min-w-0">
                              {honeytoken.region && <div>{honeytoken.region}</div>}
                              {honeytoken.account_id && <div className="font-mono">{honeytoken.account_id}</div>}
                              {honeytoken.resource_path && (
                                <div className="truncate max-w-[180px] text-gray-400">
                                  {honeytoken.resource_path.startsWith("arn:") ? <Short value={honeytoken.resource_path} /> : honeytoken.resource_path}
                                </div>
                              )}
                              {!honeytoken.region && !honeytoken.account_id && !honeytoken.resource_path && <span className="text-gray-300">—</span>}
                            </div>
                          </td>
                          <td data-label="Placement" className="px-4 py-3 text-xs">
                            {honeytoken.placement ? (
                              <span className={`inline-flex px-1.5 py-0.5 rounded font-medium ${PLACEMENT_BADGE[honeytoken.placement.type]}`}>
                                {PLACEMENT_LABELS[honeytoken.placement.type]}
                              </span>
                            ) : (
                              <span className="text-gray-300">—</span>
                            )}
                          </td>
                          <td data-label="Status" className="px-4 py-3">
                            <div className="min-w-0">
                              <div className="flex flex-wrap items-center justify-end gap-1.5 md:justify-start">
                                <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${STATUS_BADGE[honeytoken.status]}`}>
                                  {STATUS_LABELS[honeytoken.status]}
                                </span>
                                {hasAlert && (
                                  <span className="px-1.5 py-0.5 rounded-full bg-rose-200 text-rose-800 text-xs font-semibold">
                                    {honeytoken.active_alert_count} alert{honeytoken.active_alert_count > 1 ? "s" : ""}
                                  </span>
                                )}
                              </div>
                              {honeytoken.pr_url && (
                                <a
                                  href={honeytoken.pr_url}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  onClick={(e) => e.stopPropagation()}
                                  className="text-xs text-blue-600 hover:underline mt-0.5 block"
                                >
                                  View PR ↗
                                </a>
                              )}
                            </div>
                          </td>
                          <td data-label="Last triggered" className="px-4 py-3 text-xs text-gray-400">
                            <div>
                              {honeytoken.last_triggered_at ? (
                                <span className={hasAlert ? "text-rose-600 font-medium" : ""} title={exactUtc(honeytoken.last_triggered_at)}>
                                  {relativeTime(honeytoken.last_triggered_at)}
                                </span>
                              ) : (
                                <span className="text-gray-300">Never</span>
                              )}
                              {honeytoken.trigger_count > 0 && (
                                <div className="text-gray-300">{honeytoken.trigger_count} total</div>
                              )}
                            </div>
                          </td>
                          <td data-label="Actions" className="px-4 py-3 text-right">
                            <div className="flex flex-wrap items-center justify-end gap-2" onClick={(e) => e.stopPropagation()}>
                              <button
                                onClick={() => setEditHoneytoken(honeytoken)}
                                {...writeProps(readOnly)}
                                className={BTN_OUTLINE_SM}
                              >
                                Edit
                              </button>
                              {honeytoken.status !== "decommissioned" && (
                                <button
                                  onClick={() => void handleDelete(honeytoken)}
                                  {...writeProps(readOnly, deletingId === honeytoken.honeytoken_id)}
                                  className={BTN_QUIET_DANGER_SM}
                                >
                                  Decommission
                                </button>
                              )}
                              <span className="text-gray-300 text-sm">{isExpanded ? "↑" : "↓"}</span>
                            </div>
                          </td>
                        </tr>
                        {isExpanded && (
                          <tr key={`${honeytoken.honeytoken_id}-expanded`} className={hasAlert ? "bg-rose-50/40" : "bg-gray-50/40"}>
                            <td colSpan={7} className="rt-block p-0">
                              <AlertPanel
                                honeytoken={honeytoken}
                                endpoints={endpoints}
                                onClose={() => setExpandedId(null)}
                                onHoneytokenChanged={fetchHoneytokens}
                              />
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
            </div>
          </section>

          {showCreateModal && (
            <HoneytokenModal
              endpoints={endpoints}
              onClose={() => setShowCreateModal(false)}
              onSaved={fetchHoneytokens}
            />
          )}

          {editHoneytoken && (
            <HoneytokenModal
              honeytoken={editHoneytoken}
              endpoints={endpoints}
              onClose={() => setEditHoneytoken(null)}
              onSaved={fetchHoneytokens}
            />
          )}
        </>
      )}

      {/* ── History: every incident, then the audit of actions taken ── */}
      {view === "history" && (
        <>
          <HoneypotIncidents show="table" tokenInfo={tokenInfo} onLoaded={setIncidents} />
          <HoneypotAuditList defaultOpen />
        </>
      )}

      {/* ── Settings: whitelist, responder role, notifications, repo ── */}
      {view === "settings" && (
        <>
          <h2 className="sr-only">Settings</h2>
          <HoneypotActions />
          <ResponderRolePreview />
          <EndpointsPanel endpoints={endpoints} onRefresh={fetchEndpoints} />
          <RepoSelector />
        </>
      )}
    </div>
    </DemoTripContext.Provider>
    </ReadOnlyContext.Provider>
  );
}
