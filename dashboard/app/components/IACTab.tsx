"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type IACTabProps = {
  onScanTriggered?: () => void;
};

type ResourceSummary = {
  resource_id: string;
  resource_type: string;
  name: string;
  region: string;
  account_id: string;
};

type DriftField = {
  field: string;
  declared: unknown;
  actual: unknown;
};

type DriftedResource = ResourceSummary & {
  drift_fields: DriftField[];
};

type TypeBreakdown = {
  type: string;
  total: number;
  covered: number;
  drifted: number;
};

type CoverageSummary = {
  coverage_pct: number;
  total: number;
  covered: number;
  unmanaged: number;
  drifted: number;
  by_type: TypeBreakdown[];
  unmanaged_resources: ResourceSummary[];
  drifted_resources: DriftedResource[];
};

type Suggestions = {
  repos: string[];
  modules: Array<{ repo: string; path: string }>;
};

type ImportFormState = {
  repo: string;
  branch: string;
  module_path: string;
};

const RESOURCE_TYPE_LABELS: Record<string, string> = {
  lambda_function: "Lambda",
  ecs_service: "ECS Service",
  ecs_cluster: "ECS Cluster",
  ecs_task_definition: "ECS Task Def",
  ec2_instance: "EC2 Instance",
  ec2_security_group: "Security Group",
  elbv2_load_balancer: "Load Balancer",
  elbv2_target_group: "Target Group",
  dynamodb_table: "DynamoDB",
  sqs_queue: "SQS Queue",
  sns_topic: "SNS Topic",
  secretsmanager_secret: "Secret",
  iam_role: "IAM Role",
  cloudwatch_log_group: "Log Group",
  cloudwatch_alarm: "CW Alarm",
  eventbridge_rule: "EventBridge",
  stepfunctions_state_machine: "Step Fn",
};

const labelFor = (type: string) => RESOURCE_TYPE_LABELS[type] ?? type;

const coverageColor = (pct: number) => {
  if (pct >= 80) return "text-green-600";
  if (pct >= 50) return "text-yellow-600";
  return "text-red-600";
};

const actionBtn =
  "inline-flex items-center justify-center bg-blue-600 text-white text-xs font-semibold px-3 py-1.5 rounded-md transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50";

const StatCard = ({
  label,
  value,
  valueClass,
  sub,
}: {
  label: string;
  value: string | number;
  valueClass?: string;
  sub?: string;
}) => (
  <div className="rounded-lg border bg-white px-5 py-4">
    <div className="text-xs font-semibold uppercase tracking-wide text-gray-500">{label}</div>
    <div className={`mt-1 text-3xl font-bold ${valueClass ?? "text-gray-900"}`}>{value}</div>
    {sub && <div className="mt-1 text-xs text-gray-500">{sub}</div>}
  </div>
);

const truncate = (s: string, max = 40) =>
  s.length > max ? `…${s.slice(-(max - 1))}` : s;

type ActiveTable = "unmanaged" | "drifted";

function ImportModal({
  selectedIds,
  suggestions,
  onClose,
  onSubmit,
}: {
  selectedIds: string[];
  suggestions: Suggestions;
  onClose: () => void;
  onSubmit: (form: ImportFormState) => Promise<void>;
}) {
  const [form, setForm] = useState<ImportFormState>({
    repo: suggestions.repos[0] ?? "",
    branch: "main",
    module_path: "",
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [moduleDropdownOpen, setModuleDropdownOpen] = useState(false);
  const overlayRef = useRef<HTMLDivElement>(null);

  const modulesForRepo = suggestions.modules
    .filter((m) => !form.repo || m.repo === form.repo)
    .map((m) => m.path);

  const filteredModules = modulesForRepo.filter(
    (p) => !form.module_path || p.toLowerCase().includes(form.module_path.toLowerCase()),
  );

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.repo.trim()) {
      setError("Repository is required (e.g. org/repo)");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(form);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Import failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      ref={overlayRef}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      onClick={(e) => { if (e.target === overlayRef.current) onClose(); }}
    >
      <div className="w-full max-w-lg rounded-xl bg-white shadow-xl">
        <div className="flex items-center justify-between border-b px-6 py-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">Import to Terraform</h2>
            <p className="text-xs text-gray-500 mt-0.5">
              {selectedIds.length} resource{selectedIds.length !== 1 ? "s" : ""} selected
            </p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">×</button>
        </div>

        <form onSubmit={(e) => void handleSubmit(e)} className="px-6 py-5 space-y-4">
          {error && (
            <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </div>
          )}

          <div>
            <label className="block text-xs font-semibold text-gray-600 mb-1">
              Repository <span className="text-red-500">*</span>
            </label>
            <input
              list="iac-repos"
              type="text"
              placeholder="org/repo"
              value={form.repo}
              onChange={(e) => setForm((f) => ({ ...f, repo: e.target.value, module_path: "" }))}
              className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              required
            />
            <datalist id="iac-repos">
              {suggestions.repos.map((r) => <option key={r} value={r} />)}
            </datalist>
          </div>

          <div>
            <label className="block text-xs font-semibold text-gray-600 mb-1">Base branch</label>
            <input
              type="text"
              placeholder="main"
              value={form.branch}
              onChange={(e) => setForm((f) => ({ ...f, branch: e.target.value }))}
              className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          <div className="relative">
            <label className="block text-xs font-semibold text-gray-600 mb-1">
              Module path <span className="text-gray-400">(optional)</span>
            </label>
            <input
              type="text"
              placeholder={modulesForRepo.length ? "Select existing or type a new module path…" : "e.g. modules/lambda"}
              value={form.module_path}
              onFocus={() => setModuleDropdownOpen(true)}
              onBlur={() => setTimeout(() => setModuleDropdownOpen(false), 120)}
              onChange={(e) => {
                setForm((f) => ({ ...f, module_path: e.target.value }));
                setModuleDropdownOpen(true);
              }}
              className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            {moduleDropdownOpen && filteredModules.length > 0 && (
              <ul className="absolute z-20 mt-1 w-full rounded border border-gray-200 bg-white shadow-lg max-h-40 overflow-y-auto">
                {filteredModules.map((p) => (
                  <li
                    key={p}
                    onMouseDown={() => {
                      setForm((f) => ({ ...f, module_path: p }));
                      setModuleDropdownOpen(false);
                    }}
                    className="cursor-pointer px-3 py-2 text-sm text-gray-800 hover:bg-blue-50"
                  >
                    {p}
                  </li>
                ))}
              </ul>
            )}
            {modulesForRepo.length > 0 && !moduleDropdownOpen && (
              <p className="mt-1 text-xs text-gray-400">
                {modulesForRepo.length} module{modulesForRepo.length !== 1 ? "s" : ""} found. Select one or type a new path.
              </p>
            )}
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="inline-flex items-center justify-center rounded bg-blue-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {submitting ? "Queuing…" : "Create Import PR"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function FixDriftModal({
  resourceIds,
  driftedResources,
  onClose,
  onSubmit,
}: {
  resourceIds: string[];
  driftedResources: DriftedResource[];
  onClose: () => void;
  onSubmit: (ids: string[]) => Promise<void>;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const overlayRef = useRef<HTMLDivElement>(null);

  const selected = driftedResources.filter((r) => resourceIds.includes(r.resource_id));

  const handleSubmit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(resourceIds);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fix drift failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      ref={overlayRef}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      onClick={(e) => { if (e.target === overlayRef.current) onClose(); }}
    >
      <div className="w-full max-w-md rounded-xl bg-white shadow-xl">
        <div className="flex items-center justify-between border-b px-6 py-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">Fix Drift</h2>
            <p className="text-xs text-gray-500 mt-0.5">
              Generate a PR to correct {resourceIds.length} drifted resource{resourceIds.length !== 1 ? "s" : ""}
            </p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">×</button>
        </div>

        <div className="px-6 py-5 space-y-4">
          {error && (
            <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </div>
          )}

          <p className="text-sm text-gray-600">
            Compass will generate a Terraform PR that corrects the configuration to match the current AWS state. The target repository and module are auto-detected from the infra map.
          </p>

          <ul className="space-y-1 max-h-48 overflow-y-auto">
            {selected.map((r) => (
              <li key={r.resource_id} className="flex items-start gap-2 text-sm">
                <span className="mt-1 h-2 w-2 flex-shrink-0 rounded-full bg-red-500" />
                <div>
                  <span className="font-medium text-gray-900">{r.name}</span>
                  <span className="ml-2 text-xs text-gray-400">
                    {r.drift_fields.length} field{r.drift_fields.length !== 1 ? "s" : ""} drifted
                  </span>
                </div>
              </li>
            ))}
          </ul>

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              onClick={() => void handleSubmit()}
              disabled={submitting}
              className="inline-flex items-center justify-center rounded bg-blue-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {submitting ? "Queuing…" : "Create Fix PR"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function IACTab({ onScanTriggered }: IACTabProps) {
  const [data, setData] = useState<CoverageSummary | null>(null);
  const [suggestions, setSuggestions] = useState<Suggestions>({ repos: [], modules: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [activeTable, setActiveTable] = useState<ActiveTable>("unmanaged");
  const [expandedDrift, setExpandedDrift] = useState<string | null>(null);
  const [selectedUnmanagedIds, setSelectedUnmanagedIds] = useState<Set<string>>(new Set());
  const [selectedDriftedIds, setSelectedDriftedIds] = useState<Set<string>>(new Set());
  const [showImportModal, setShowImportModal] = useState(false);
  const [showFixDriftModal, setShowFixDriftModal] = useState(false);
  const [importSuccess, setImportSuccess] = useState<{ jobId: string; count: number } | null>(null);
  const [fixDriftSuccess, setFixDriftSuccess] = useState<{ jobId: string; count: number } | null>(null);
  const [typeTableExpanded, setTypeTableExpanded] = useState(true);
  const [unmanagedSearch, setUnmanagedSearch] = useState("");

  const fetchCoverage = useCallback(async () => {
    try {
      const [coverageRes, suggestionsRes] = await Promise.all([
        fetch("/api/iac/coverage"),
        fetch("/api/iac/suggestions"),
      ]);
      if (!coverageRes.ok) throw new Error(`HTTP ${coverageRes.status}`);
      setData((await coverageRes.json()) as CoverageSummary);
      if (suggestionsRes.ok) {
        setSuggestions((await suggestionsRes.json()) as Suggestions);
      }
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load IAC coverage");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchCoverage();
  }, [fetchCoverage]);

  const handleScan = async () => {
    setScanning(true);
    try {
      const res = await fetch("/api/iac/scan", { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      onScanTriggered?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to trigger scan");
    } finally {
      setScanning(false);
    }
  };

  const handleImportSubmit = async (form: ImportFormState) => {
    const ids = Array.from(selectedUnmanagedIds);
    const res = await fetch("/api/iac/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        resource_ids: ids,
        repo: form.repo.trim(),
        branch: form.branch.trim() || "main",
        module_path: form.module_path.trim() || null,
      }),
    });
    if (!res.ok) {
      const detail = (await res.json().catch(() => null))?.detail;
      throw new Error(detail ?? `HTTP ${res.status}`);
    }
    const job = (await res.json()) as { job_id: string; resource_count: number };
    setImportSuccess({ jobId: job.job_id, count: job.resource_count });
    setShowImportModal(false);
    setSelectedUnmanagedIds(new Set());
  };

  const handleFixDriftSubmit = async (resourceIds: string[]) => {
    const res = await fetch("/api/iac/fix-drift", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resource_ids: resourceIds }),
    });
    if (!res.ok) {
      const detail = (await res.json().catch(() => null))?.detail;
      throw new Error(detail ?? `HTTP ${res.status}`);
    }
    const job = (await res.json()) as { job_id: string; resource_count: number };
    setFixDriftSuccess({ jobId: job.job_id, count: job.resource_count });
    setShowFixDriftModal(false);
    setSelectedDriftedIds(new Set());
  };

  const toggleSelectUnmanaged = (id: string) =>
    setSelectedUnmanagedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const toggleSelectDrifted = (id: string) =>
    setSelectedDriftedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const toggleSelectAllUnmanaged = (resources: ResourceSummary[]) => {
    const allIds = resources.map((r) => r.resource_id);
    const allSelected = allIds.length > 0 && allIds.every((id) => selectedUnmanagedIds.has(id));
    setSelectedUnmanagedIds(allSelected ? new Set() : new Set(allIds));
  };

  const toggleSelectAllDrifted = (resources: DriftedResource[]) => {
    const allIds = resources.map((r) => r.resource_id);
    const allSelected = allIds.length > 0 && allIds.every((id) => selectedDriftedIds.has(id));
    setSelectedDriftedIds(allSelected ? new Set() : new Set(allIds));
  };

  const filteredUnmanaged = data
    ? unmanagedSearch.trim()
      ? data.unmanaged_resources.filter((r) => {
          const q = unmanagedSearch.toLowerCase();
          return (
            r.name.toLowerCase().includes(q) ||
            r.resource_type.toLowerCase().includes(q) ||
            r.resource_id.toLowerCase().includes(q)
          );
        })
      : data.unmanaged_resources
    : [];

  return (
    <div className="p-6 space-y-6">
      {showImportModal && (
        <ImportModal
          selectedIds={Array.from(selectedUnmanagedIds)}
          suggestions={suggestions}
          onClose={() => setShowImportModal(false)}
          onSubmit={handleImportSubmit}
        />
      )}

      {showFixDriftModal && data && (
        <FixDriftModal
          resourceIds={Array.from(selectedDriftedIds)}
          driftedResources={data.drifted_resources}
          onClose={() => setShowFixDriftModal(false)}
          onSubmit={handleFixDriftSubmit}
        />
      )}

      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">IAC Coverage</h1>
          <p className="mt-1 text-sm text-gray-500">
            Terraform coverage and drift across your AWS infrastructure.
          </p>
        </div>
        <button
          onClick={() => void handleScan()}
          disabled={scanning}
          className="rounded bg-gray-900 px-4 py-2 text-sm font-semibold text-white hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {scanning ? "Scanning…" : "Re-run IAC Scan"}
        </button>
      </div>

      {/* Error */}
      {error && (
        <div className="rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {/* Import queued banner */}
      {importSuccess && (
        <div className="flex items-center justify-between rounded border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-800">
          <span>
            Import PR queued for {importSuccess.count} resource{importSuccess.count !== 1 ? "s" : ""}.{" "}
            Job: <span className="font-mono">{importSuccess.jobId}</span>
          </span>
          <button onClick={() => setImportSuccess(null)} className="ml-4 text-green-600 hover:text-green-800">×</button>
        </div>
      )}

      {/* Drift fix queued banner */}
      {fixDriftSuccess && (
        <div className="flex items-center justify-between rounded border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-800">
          <span>
            Drift fix PR queued for {fixDriftSuccess.count} resource{fixDriftSuccess.count !== 1 ? "s" : ""}.{" "}
            Job: <span className="font-mono">{fixDriftSuccess.jobId}</span>
          </span>
          <button onClick={() => setFixDriftSuccess(null)} className="ml-4 text-green-600 hover:text-green-800">×</button>
        </div>
      )}

      {/* Loading skeleton */}
      {loading && !data && (
        <div className="grid grid-cols-4 gap-4">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="h-24 animate-pulse rounded-lg bg-gray-100" />
          ))}
        </div>
      )}

      {data && (
        <>
          {/* Stats */}
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <StatCard
              label="Coverage"
              value={`${data.coverage_pct}%`}
              valueClass={coverageColor(data.coverage_pct)}
              sub={`${data.covered} of ${data.total} resources`}
            />
            <StatCard label="Managed" value={data.covered} sub="in Terraform state" />
            <StatCard
              label="Unmanaged"
              value={data.unmanaged}
              valueClass={data.unmanaged > 0 ? "text-yellow-600" : "text-gray-900"}
              sub="not in any state file"
            />
            <StatCard
              label="Drifted"
              value={data.drifted}
              valueClass={data.drifted > 0 ? "text-red-600" : "text-gray-900"}
              sub="attributes differ from state"
            />
          </div>

          {/* By-type breakdown — collapsible */}
          {data.by_type.length > 0 && (
            <div className="rounded-lg border bg-white overflow-hidden">
              <button
                onClick={() => setTypeTableExpanded((v) => !v)}
                className="flex w-full items-center justify-between px-4 py-3 border-b text-left bg-[#f8f9fa] hover:bg-[#e9ecef] transition-colors"
              >
                <h2 className="text-sm font-semibold text-gray-700">Coverage by resource type</h2>
                <span className="text-gray-400 text-xs">{typeTableExpanded ? "▲ Collapse" : "▼ Expand"}</span>
              </button>
              {typeTableExpanded && (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-gray-50 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
                        <th className="px-4 py-2">Type</th>
                        <th className="px-4 py-2 text-right">Total</th>
                        <th className="px-4 py-2 text-right">Covered</th>
                        <th className="px-4 py-2 text-right">Unmanaged</th>
                        <th className="px-4 py-2 text-right">Drifted</th>
                        <th className="px-4 py-2">Coverage</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100">
                      {data.by_type.map((row) => {
                        const pct = row.total > 0 ? Math.round((row.covered / row.total) * 100) : 0;
                        return (
                          <tr key={row.type} className="hover:bg-gray-50">
                            <td className="px-4 py-2 font-medium text-gray-800">{labelFor(row.type)}</td>
                            <td className="px-4 py-2 text-right text-gray-600">{row.total}</td>
                            <td className="px-4 py-2 text-right text-gray-600">{row.covered}</td>
                            <td className="px-4 py-2 text-right text-gray-600">{row.total - row.covered}</td>
                            <td className={`px-4 py-2 text-right font-medium ${row.drifted > 0 ? "text-red-600" : "text-gray-400"}`}>
                              {row.drifted}
                            </td>
                            <td className="px-4 py-2">
                              <div className="flex items-center gap-2">
                                <div className="h-1.5 w-24 rounded-full bg-gray-200">
                                  <div className="h-1.5 rounded-full bg-blue-500" style={{ width: `${pct}%` }} />
                                </div>
                                <span className={`text-xs font-medium ${coverageColor(pct)}`}>{pct}%</span>
                              </div>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* Table toggle tabs */}
          <div className="flex gap-2">
            {(["unmanaged", "drifted"] as ActiveTable[]).map((tab) => (
              <button
                key={tab}
                onClick={() => setActiveTable(tab)}
                className="px-4 py-2 text-sm font-semibold rounded text-gray-700 transition-colors bg-[#f8f9fa] hover:bg-[#e9ecef]"
              >
                {tab === "unmanaged" ? `Unmanaged (${data.unmanaged})` : `Drifted (${data.drifted})`}
              </button>
            ))}
          </div>

          {/* Unmanaged resources */}
          {activeTable === "unmanaged" && (
            <div className="space-y-2">
              {/* Toolbar */}
              <div className="flex items-center gap-3">
                <input
                  type="text"
                  placeholder="Search by name, type, or ID…"
                  value={unmanagedSearch}
                  onChange={(e) => setUnmanagedSearch(e.target.value)}
                  className="flex-1 rounded border border-gray-300 px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                {selectedUnmanagedIds.size > 0 && (
                  <button onClick={() => setShowImportModal(true)} className={actionBtn}>
                    Import {selectedUnmanagedIds.size} selected
                  </button>
                )}
              </div>

              <div className="rounded-lg border bg-white overflow-hidden">
                {data.unmanaged_resources.length === 0 ? (
                  <div className="px-6 py-10 text-center text-sm text-gray-500">
                    All resources are managed by Terraform.
                  </div>
                ) : filteredUnmanaged.length === 0 ? (
                  <div className="px-6 py-10 text-center text-sm text-gray-500">
                    No resources match your search.
                  </div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="bg-gray-50 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
                          <th className="px-4 py-2">
                            <input
                              type="checkbox"
                              className="h-4 w-4 rounded border-gray-300"
                              checked={
                                filteredUnmanaged.length > 0 &&
                                filteredUnmanaged.every((r) => selectedUnmanagedIds.has(r.resource_id))
                              }
                              onChange={() => toggleSelectAllUnmanaged(filteredUnmanaged)}
                            />
                          </th>
                          <th className="px-4 py-2">Name</th>
                          <th className="px-4 py-2">Type</th>
                          <th className="px-4 py-2">Region</th>
                          <th className="px-4 py-2">Account</th>
                          <th className="px-4 py-2">Actions</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-100">
                        {filteredUnmanaged.map((r) => (
                          <tr
                            key={r.resource_id}
                            className={`hover:bg-gray-50 ${selectedUnmanagedIds.has(r.resource_id) ? "bg-blue-50" : ""}`}
                          >
                            <td className="px-4 py-2">
                              <input
                                type="checkbox"
                                className="h-4 w-4 rounded border-gray-300"
                                checked={selectedUnmanagedIds.has(r.resource_id)}
                                onChange={() => toggleSelectUnmanaged(r.resource_id)}
                              />
                            </td>
                            <td className="px-4 py-2">
                              <div className="font-medium text-gray-900">{r.name}</div>
                              <div className="font-mono text-[10px] text-gray-400">{truncate(r.resource_id)}</div>
                            </td>
                            <td className="px-4 py-2 text-gray-600">{labelFor(r.resource_type)}</td>
                            <td className="px-4 py-2 text-gray-600">{r.region}</td>
                            <td className="px-4 py-2 font-mono text-xs text-gray-500">{r.account_id}</td>
                            <td className="px-4 py-2">
                              <button
                                className={actionBtn}
                                onClick={() => {
                                  setSelectedUnmanagedIds(new Set([r.resource_id]));
                                  setShowImportModal(true);
                                }}
                              >
                                Import
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Drifted resources */}
          {activeTable === "drifted" && (
            <div className="space-y-2">
              {/* Toolbar */}
              {selectedDriftedIds.size > 0 && (
                <div className="flex justify-end">
                  <button onClick={() => setShowFixDriftModal(true)} className={actionBtn}>
                    Fix {selectedDriftedIds.size} selected
                  </button>
                </div>
              )}

              <div className="rounded-lg border bg-white overflow-hidden">
                {data.drifted_resources.length === 0 ? (
                  <div className="px-6 py-10 text-center text-sm text-gray-500">
                    No drift detected across managed resources.
                  </div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="bg-gray-50 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
                          <th className="px-4 py-2">
                            <input
                              type="checkbox"
                              className="h-4 w-4 rounded border-gray-300"
                              checked={
                                data.drifted_resources.length > 0 &&
                                data.drifted_resources.every((r) => selectedDriftedIds.has(r.resource_id))
                              }
                              onChange={() => toggleSelectAllDrifted(data.drifted_resources)}
                            />
                          </th>
                          <th className="px-4 py-2">Name</th>
                          <th className="px-4 py-2">Type</th>
                          <th className="px-4 py-2">Region</th>
                          <th className="px-4 py-2">Drifted Fields</th>
                          <th className="px-4 py-2">Actions</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-100">
                        {data.drifted_resources.map((r) => {
                          const isExpanded = expandedDrift === r.resource_id;
                          return (
                            <tr
                              key={r.resource_id}
                              className={`hover:bg-gray-50 ${selectedDriftedIds.has(r.resource_id) ? "bg-blue-50" : ""}`}
                            >
                              <td className="px-4 py-2" onClick={(e) => e.stopPropagation()}>
                                <input
                                  type="checkbox"
                                  className="h-4 w-4 rounded border-gray-300"
                                  checked={selectedDriftedIds.has(r.resource_id)}
                                  onChange={() => toggleSelectDrifted(r.resource_id)}
                                />
                              </td>
                              <td
                                className="px-4 py-2 cursor-pointer"
                                onClick={() => setExpandedDrift(isExpanded ? null : r.resource_id)}
                              >
                                <div className="font-medium text-gray-900">{r.name}</div>
                                <div className="font-mono text-[10px] text-gray-400">{truncate(r.resource_id)}</div>
                                {isExpanded && (
                                  <table className="mt-2 w-full text-xs">
                                    <thead>
                                      <tr className="text-left text-gray-500">
                                        <th className="pb-1 pr-8 font-semibold">Field</th>
                                        <th className="pb-1 pr-8 font-semibold">Declared (state)</th>
                                        <th className="pb-1 font-semibold">Actual (AWS)</th>
                                      </tr>
                                    </thead>
                                    <tbody className="divide-y divide-gray-100">
                                      {r.drift_fields.map((df) => (
                                        <tr key={df.field}>
                                          <td className="py-1 pr-8 font-mono text-gray-700">{df.field}</td>
                                          <td className="py-1 pr-8 font-mono text-blue-700">{String(df.declared)}</td>
                                          <td className="py-1 font-mono text-red-700">{String(df.actual)}</td>
                                        </tr>
                                      ))}
                                    </tbody>
                                  </table>
                                )}
                              </td>
                              <td className="px-4 py-2 text-gray-600">{labelFor(r.resource_type)}</td>
                              <td className="px-4 py-2 text-gray-600">{r.region}</td>
                              <td className="px-4 py-2">
                                <span className="inline-flex items-center gap-1 rounded bg-red-50 px-2 py-0.5 text-xs font-medium text-red-700">
                                  {r.drift_fields.length} field{r.drift_fields.length !== 1 ? "s" : ""}
                                </span>
                              </td>
                              <td className="px-4 py-2" onClick={(e) => e.stopPropagation()}>
                                <button
                                  className={actionBtn}
                                  onClick={() => {
                                    setSelectedDriftedIds(new Set([r.resource_id]));
                                    setShowFixDriftModal(true);
                                  }}
                                >
                                  Fix Drift
                                </button>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
