"use client";
// The open captains' tabs and settings components, in one place: what the
// product does in app/product/index.ts for its own. DashboardClient renders
// the component registered for the active tab key; the API's bootstrap decides
// which keys are enabled (compass_core.captains).
import dynamic from "next/dynamic";
import React from "react";
import type { CaptainSettingsComponent, CaptainTab } from "./extensions.open";
import { useShell } from "./shell";

const loading = (what: string) => () => <div className="p-6 text-sm text-gray-500">Loading {what}…</div>;

const MapsTabView = dynamic(() => import("../components/MapsTab"), { ssr: false, loading: loading("maps") });
const IACTabView = dynamic(() => import("../components/IACTab"), { ssr: false, loading: loading("IAC coverage") });
const PRsTabView = dynamic(() => import("../components/PRsTab"), { ssr: false, loading: loading("PRs") });
const HoneypotsTabView = dynamic(() => import("../components/HoneypotsTab"), { ssr: false, loading: loading("honeypots") });
const QueueTabView = dynamic(() => import("../components/QueueTab"), { ssr: false, loading: loading("queue") });
const PRWatcherSettingsView = dynamic(() => import("../components/PRWatcherSettings"), { ssr: false });

// Maps and IAC follow the mapper job and refresh the queue and the bootstrap when they start one.
function MapsTab() {
  const { infraScan, activeMapperJob, fetchQueueJobs, refreshSettingsBootstrap } = useShell();
  return (
    <MapsTabView
      infraScan={infraScan}
      mapperJob={activeMapperJob}
      onMapperTriggered={() => {
        void fetchQueueJobs();
        void refreshSettingsBootstrap();
      }}
    />
  );
}

function IACTab() {
  const { fetchQueueJobs, refreshSettingsBootstrap } = useShell();
  return (
    <IACTabView
      onScanTriggered={() => {
        void fetchQueueJobs();
        void refreshSettingsBootstrap();
      }}
    />
  );
}

// The same keys, labels and groups as the manifests (captains/<name>/manifest.py);
// the bootstrap's `app.tabs` is what the sidebar draws from, this is what renders.
export const openTabs: CaptainTab[] = [
  { key: "maps", label: "Maps", feature: "maps", group: "platform", Component: MapsTab },
  { key: "iac", label: "IAC", feature: "iac", group: "cost", Component: IACTab },
  { key: "prs", label: "Pull requests", feature: "prs", group: "platform", views: ["open", "needs_you", "history"], Component: PRsTabView as React.ComponentType },
  { key: "honeypots", label: "Decoys", feature: "honeypots", group: "security", views: ["overview", "honeytokens", "needs_you", "history", "settings"], Component: HoneypotsTabView as React.ComponentType },
  { key: "queue", label: "Queue", feature: "queue", group: "platform", views: ["jobs", "needs_you"], Component: QueueTabView as React.ComponentType },
];

// A captain's own settings UI, by the manifest's `settings_component` key.
export const openSettingsComponents: Record<string, CaptainSettingsComponent> = {
  prs: PRWatcherSettingsView as unknown as CaptainSettingsComponent,
};
