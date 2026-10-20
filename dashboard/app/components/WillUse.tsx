// What a captain will use, from its manifest's permissions block (compass_core.permissions.describe):
// the AWS reads grouped by service, the write actions it may propose (each needs a grant a person
// accepts and a merged pull request before it can run), its GitHub scopes and the hosts it reaches.
// Shown on the Captains page before anything is installed.
import React from "react";

export type PermissionsSummary = {
  declared: boolean;
  reads: Record<string, string[]>;
  scoped?: Array<{ actions: string[]; resources: string[] }>;
  actions: Array<string | { action_type: string; title?: string; change_type?: string; rollback_supported?: boolean; registered?: boolean }>;
  github: string[];
  egress: string[];
};

export function WillUse({ permissions, compact }: { permissions?: PermissionsSummary | null; compact?: boolean }) {
  if (!permissions) return null;
  if (!permissions.declared) {
    return <p className="mt-1 text-xs text-amber-700">Declares no permissions: `compass validate` refuses it, and its sessions can read nothing.</p>;
  }
  const services = Object.keys(permissions.reads);
  const actions = permissions.actions.map((a) => (typeof a === "string" ? { action_type: a } : a));
  const nothing = services.length === 0 && actions.length === 0 && permissions.github.length === 0 && permissions.egress.length === 0;
  if (nothing) return <p className="mt-1 text-xs text-gray-400">Will use: nothing in AWS, nothing outside Compass.</p>;
  return (
    <div className={`mt-1 text-xs text-gray-500 ${compact ? "" : "space-y-0.5"}`}>
      {services.length > 0 && (
        <p>
          <span className="font-medium text-gray-600">Reads:</span>{" "}
          {services.map((s) => `${s} (${permissions.reads[s].join(", ")})`).join(" · ")}
          {permissions.scoped && permissions.scoped.length > 0 && (
            <span className="text-gray-400"> · scoped: {permissions.scoped.map((s) => `${s.actions.join(", ")} on ${s.resources.join(", ")}`).join("; ")}</span>
          )}
        </p>
      )}
      {actions.length > 0 && (
        <p>
          <span className="font-medium text-gray-600">Can propose {actions.length} write action{actions.length === 1 ? "" : "s"}:</span>{" "}
          {actions.map((a) => `${a.title || a.action_type}${a.change_type ? ` (${a.change_type}${a.rollback_supported ? ", rollback" : ""})` : ""}`).join(" · ")}
          <span className="text-gray-400"> — each needs your approval and a merged pull request before it can run</span>
        </p>
      )}
      {(permissions.github.length > 0 || permissions.egress.length > 0) && (
        <p>
          {permissions.github.length > 0 && <><span className="font-medium text-gray-600">GitHub:</span> {permissions.github.join(", ")}</>}
          {permissions.github.length > 0 && permissions.egress.length > 0 && " · "}
          {permissions.egress.length > 0 && <><span className="font-medium text-gray-600">Reaches:</span> {permissions.egress.join(", ")}</>}
        </p>
      )}
    </div>
  );
}
