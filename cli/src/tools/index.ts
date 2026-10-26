import type { ToolDef } from "../core/registry.js";
import { accessTool } from "./access.js";
import { accountTool, githubTool } from "./account.js";
import { captainsTool, extensionsTool } from "./captains.js";
import { honeypotsDecommissionTool, honeypotsIncidentTool, honeypotsManageTool, honeypotsPlantTool, honeypotsRunbookTool, honeypotsTool } from "./honeypots.js";
import { iacFixDriftTool, iacImportTool, iacTool } from "./iac.js";
import { inboxActTool, inboxSnoozeTool, inboxTool } from "./inbox.js";
import { jobsTool } from "./jobs.js";
import { mapTool } from "./map.js";
import { notifyEndpointsTool, sesSenderTool } from "./notify.js";
import { patchFeedTool, patchFindingsTool, patchIgnoreTool, patchPrTool } from "./patch.js";
import { prsTool, prsWatcherTool } from "./prs.js";
import { doctorTool, statusTool } from "./status.js";
import { tokensTool } from "./tokens.js";

/** The tools of the open edition. Plugins add theirs (src/core/plugins.ts). */
export const TOOLS: ToolDef<any>[] = [
  statusTool,
  doctorTool,
  inboxTool,
  inboxActTool,
  inboxSnoozeTool,
  accountTool,
  accessTool,
  githubTool,
  tokensTool,
  mapTool,
  jobsTool,
  prsTool,
  prsWatcherTool,
  captainsTool,
  extensionsTool,
  iacTool,
  iacImportTool,
  iacFixDriftTool,
  honeypotsTool,
  honeypotsPlantTool,
  honeypotsManageTool,
  honeypotsRunbookTool,
  honeypotsDecommissionTool,
  honeypotsIncidentTool,
  notifyEndpointsTool,
  sesSenderTool,
  patchFindingsTool,
  patchPrTool,
  patchIgnoreTool,
  patchFeedTool,
];

export function findTool(name: string): ToolDef<any> | undefined {
  return TOOLS.find((t) => t.name === name);
}
