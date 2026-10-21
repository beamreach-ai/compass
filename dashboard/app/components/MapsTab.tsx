"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import axios from "axios";
import Graphin, { Behaviors, Components as GraphinComponents } from "@antv/graphin";
import "@antv/graphin/dist/index.css";
import MappingProgressBar from "./MappingProgressBar";
import { isDemoApp } from "../lib/demo";
import { completeTourStep } from "../lib/tour";
import { useShell } from "../lib/shell";

type InfraNode = {
  org_id: string;
  resource_id: string;
  resource_type: string;
  account_id: string;
  region: string;
  name: string;
  tags: Record<string, string>;
  attributes: Record<string, any>;
  last_seen?: string;
};

type InfraEdge = {
  org_id: string;
  edge_id?: string | null;
  from_resource_id: string;
  to_resource_id: string;
  relationship_type: string;
  confidence?: string;
  evidence?: string[];
  attributes?: Record<string, any>;
  last_seen?: string;
};

type InfraMapResponse = {
  org_id: string;
  focus?: string | null;
  nodes: InfraNode[];
  edges: InfraEdge[];
};

type MapperRunResponse = {
  status: string;
  job_id: string;
  captain: "mapper";
  command: "run";
  mode: string;
  existing: boolean;
};

type JobStatusResponse = {
  job_id: string;
  captain: string;
  command: string;
  status: string;
  error?: string | null;
};

type MapperTimelineEvent = {
  timestamp?: number;
  message?: string;
  status?: string | null;
  type?: string;
  data?: Record<string, any> | null;
};

type InfraScanSummary = {
  scanned: boolean;
  integrations_ready: boolean;
  status?: string | null;
  job_id?: string | null;
  job_status?: string | null;
  latest_message?: string | null;
  last_completed_at?: string | null;
  last_failed_at?: string | null;
  last_error?: string | null;
  disabled_reason?: string | null;
};

type MapperJobSummary = {
  job_id: string;
  status: string;
  error?: string | null;
  events?: MapperTimelineEvent[];
};

type MapsTabProps = {
  infraScan?: InfraScanSummary | null;
  mapperJob?: MapperJobSummary | null;
  onMapperTriggered?: () => void;
};

type OverlayMode = "activity" | "coverage" | "traffic";
type ResourceFilterMap = Record<string, boolean>;

const RESOURCE_TYPE_COLORS: Record<string, string> = {
  cloudwatch_alarm: "#ef4444",
  cloudwatch_log_group: "#64748b",
  dynamodb_stream: "#0d9488",
  ecs_service: "#2563eb",
  ecs_cluster: "#06b6d4",
  ecs_task_definition: "#7c3aed",
  ec2_network_interface: "#14b8a6",
  ec2_security_group: "#f97316",
  elbv2_load_balancer: "#0ea5e9",
  elbv2_target_group: "#ec4899",
  eventbridge_rule: "#475569",
  secretsmanager_secret: "#f97316",
  sqs_queue: "#059669",
  sns_topic: "#d946ef",
  dynamodb_table: "#14b8a6",
  lambda_function: "#f59e0b",
  stepfunctions_state_machine: "#94a3b8",
};

const OVERLAY_ACCENTS: Record<OverlayMode, string> = {
  activity: "#f97316",
  coverage: "#0ea5e9",
  traffic: "#22c55e",
};

const RESOURCE_TYPE_SIZES: Record<string, number> = {
  cloudwatch_alarm: 18,
  cloudwatch_log_group: 18,
  ec2_instance: 28,
  ec2_network_interface: 20,
  ec2_security_group: 20,
  ecs_cluster: 28,
  ecs_service: 26,
  ecs_task_definition: 24,
  rds_db_instance: 26,
  elbv2_load_balancer: 24,
  elbv2_target_group: 22,
  lambda_function: 22,
  dynamodb_table: 24,
};

const RESOURCE_TYPE_CATEGORIES: Record<
  string,
  "compute" | "network" | "storage" | "identity" | "integration" | "security"
> = {
  cloudwatch_alarm: "security",
  cloudwatch_log_group: "storage",
  dynamodb_stream: "storage",
  dynamodb_table: "storage",
  ec2_network_interface: "network",
  ec2_security_group: "security",
  ec2_instance: "compute",
  ecs_cluster: "compute",
  ecs_service: "compute",
  ecs_task_definition: "compute",
  lambda_function: "compute",
  stepfunctions_state_machine: "integration",
  elbv2_load_balancer: "network",
  elbv2_target_group: "network",
  vpc_endpoint: "network",
  rds_db_instance: "storage",
  secretsmanager_secret: "security",
  iam_role: "identity",
  organizations_account: "identity",
  sns_topic: "integration",
  sqs_queue: "integration",
  eventbridge_rule: "integration",
};

const CATEGORY_STYLE: Record<
  string,
  { label: string; fill: string; stroke: string; accent: string }
> = {
  compute: { label: "Compute", fill: "rgba(34,197,94,0.12)", stroke: "rgba(22,163,74,0.55)", accent: "#15803d" },
  network: { label: "Network", fill: "rgba(59,130,246,0.12)", stroke: "rgba(37,99,235,0.55)", accent: "#1d4ed8" },
  storage: { label: "Storage", fill: "rgba(248,113,113,0.12)", stroke: "rgba(220,38,38,0.55)", accent: "#b91c1c" },
  identity: { label: "Identity", fill: "rgba(168,85,247,0.12)", stroke: "rgba(147,51,234,0.5)", accent: "#7e22ce" },
  integration: { label: "Integration", fill: "rgba(249,115,22,0.12)", stroke: "rgba(234,88,12,0.55)", accent: "#c2410c" },
  security: { label: "Security", fill: "rgba(14,165,233,0.12)", stroke: "rgba(8,145,178,0.6)", accent: "#0e7490" },
};

const ALWAYS_VISIBLE_LABEL_TYPES = new Set(["aws_region", "aws_vpc"]);

const VPC_ATTRIBUTE_KEYS = ["vpc_id", "vpcId", "VpcId", "vpcID", "VpcID"];
const SUBNET_ATTRIBUTE_KEYS = ["subnet_id", "subnetId", "SubnetId", "subnetID", "SubnetID"];

type ComboType = "region" | "vpc" | "subnet" | "category";
const KNOWN_COMBO_TYPES: ComboType[] = ["region", "vpc", "subnet", "category"];

type GraphinCombo = {
  id: string;
  type?: string;
  label: string;
  parentId?: string;
  data?: { type: ComboType; value?: string; region?: string; account_id?: string; category?: string };
  style?: {
    keyshape?: {
      fill?: string;
      stroke?: string;
      lineWidth?: number;
      fillOpacity?: number;
      opacity?: number;
      r?: number;
      padding?: number;
    };
    label?: {
      fill?: string;
      fontSize?: number;
      fontWeight?: number;
    };
  };
};

const COMBO_STYLE: Record<ComboType, NonNullable<GraphinCombo["style"]>> = {
  region: {
    keyshape: {
      fill: "rgba(59,130,246,0.06)",
      stroke: "rgba(37,99,235,0.35)",
      lineWidth: 1,
      padding: 24,
      r: 12,
    },
    label: {
      fill: "#1d4ed8",
      fontSize: 14,
      fontWeight: 600,
    },
  },
  vpc: {
    keyshape: {
      fill: "rgba(16,185,129,0.08)",
      stroke: "rgba(16,185,129,0.45)",
      lineWidth: 1,
      padding: 18,
      r: 10,
    },
    label: {
      fill: "#047857",
      fontSize: 12,
      fontWeight: 600,
    },
  },
  subnet: {
    keyshape: {
      fill: "rgba(251,191,36,0.08)",
      stroke: "rgba(245,158,11,0.45)",
      lineWidth: 1,
      padding: 12,
      r: 8,
    },
    label: {
      fill: "#b45309",
      fontSize: 11,
      fontWeight: 600,
    },
  },
  category: {
    keyshape: {
      fill: "rgba(148,163,184,0.12)",
      stroke: "rgba(100,116,139,0.4)",
      lineWidth: 1,
      padding: 10,
      r: 6,
    },
    label: {
      fill: "#475569",
      fontSize: 11,
      fontWeight: 600,
    },
  },
};

const PRIORITY_CATEGORIES = new Set(["identity", "network", "security"]);
const PRIORITY_CATEGORY_PADDING = 220;
const VPC_EXTRA_PADDING = 130;

const COMBO_LEVEL_PADDING: Record<ComboType, number> = {
  region: 460,
  vpc: 280,
  subnet: 190,
  category: 160,
};
const ROOT_COMBO_EXTRA_PADDING = 400;

type ComboBBox = {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
};

const computeComboSeparation = (
  boxA: ComboBBox,
  boxB: ComboBBox,
  padding: number,
): { dx: number; dy: number } | null => {
  const centerAx = (boxA.minX + boxA.maxX) / 2;
  const centerAy = (boxA.minY + boxA.maxY) / 2;
  const centerBx = (boxB.minX + boxB.maxX) / 2;
  const centerBy = (boxB.minY + boxB.maxY) / 2;
  const halfWidthA = (boxA.maxX - boxA.minX) / 2;
  const halfHeightA = (boxA.maxY - boxA.minY) / 2;
  const halfWidthB = (boxB.maxX - boxB.minX) / 2;
  const halfHeightB = (boxB.maxY - boxB.minY) / 2;

  const requiredXGap = halfWidthA + halfWidthB + padding;
  const requiredYGap = halfHeightA + halfHeightB + padding;
  const deltaX = requiredXGap - Math.abs(centerAx - centerBx);
  const deltaY = requiredYGap - Math.abs(centerAy - centerBy);

  const overlaps: Array<{ axis: "x" | "y"; value: number }> = [];
  if (deltaX > 0) overlaps.push({ axis: "x", value: deltaX });
  if (deltaY > 0) overlaps.push({ axis: "y", value: deltaY });
  if (overlaps.length === 0) {
    return null;
  }
  overlaps.sort((a, b) => b.value - a.value);
  const chosen = overlaps[0];
  if (chosen.axis === "x") {
    const direction = centerAx <= centerBx ? 1 : -1;
    return { dx: direction * chosen.value, dy: 0 };
  }
  const direction = centerAy <= centerBy ? 1 : -1;
  return { dx: 0, dy: direction * chosen.value };
};

const resolveComboType = (combo: GraphinCombo): ComboType =>
  KNOWN_COMBO_TYPES.includes(combo.data?.type as ComboType) ? (combo.data?.type as ComboType) : "category";

const comboPairPadding = (comboA: GraphinCombo, comboB: GraphinCombo, spacingScale = 1): number => {
  const typeA = resolveComboType(comboA);
  const typeB = resolveComboType(comboB);
  let padding = Math.max(COMBO_LEVEL_PADDING[typeA], COMBO_LEVEL_PADDING[typeB]);

  if (typeA === "vpc" && typeB === "vpc") {
    padding += VPC_EXTRA_PADDING;
  }

  const categoryA = comboA.data?.category;
  const categoryB = comboB.data?.category;
  if ((categoryA && PRIORITY_CATEGORIES.has(categoryA)) || (categoryB && PRIORITY_CATEGORIES.has(categoryB))) {
    padding = Math.max(padding, PRIORITY_CATEGORY_PADDING);
  }

  const bothRoot = !comboA.parentId && !comboB.parentId;
  if (bothRoot) {
    padding = Math.max(padding, ROOT_COMBO_EXTRA_PADDING);
  }

  return padding * spacingScale;
};

const sanitizeId = (value: string): string =>
  value
    .toString()
    .trim()
    .replace(/[^a-zA-Z0-9:_-]/g, "_")
    .toLowerCase();

const readAttribute = (node: InfraNode, keys: string[]): string | null => {
  for (const key of keys) {
    const value = node.attributes?.[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }
  return null;
};

const formatIsoDateTime = (value?: string | null): string => {
  if (!value) return "—";
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
};

const buildGraphinCombos = (
  nodes: InfraNode[],
): { combos: GraphinCombo[]; assignments: Record<string, string> } => {
  const comboMap = new Map<string, GraphinCombo>();
  const assignments: Record<string, string> = {};
  const locationAssignments: Record<string, string | null> = {};

  for (const node of nodes) {
    const baseRegionLabel = node.region || "unknown-region";
    const accountLabel = node.account_id || "unknown-account";
    const regionComboId = node.region
      ? sanitizeId(`region:${accountLabel}:${baseRegionLabel}`)
      : null;

    if (regionComboId && !comboMap.has(regionComboId)) {
      comboMap.set(regionComboId, {
        id: regionComboId,
        type: "rect",
        label: `${baseRegionLabel} · ${accountLabel}`,
        data: { type: "region", region: node.region, account_id: node.account_id },
        style: COMBO_STYLE.region,
      });
    }

    const vpcValue = readAttribute(node, VPC_ATTRIBUTE_KEYS);
    const vpcComboId = vpcValue ? sanitizeId(`vpc:${vpcValue}`) : null;
    if (vpcComboId && vpcValue && !comboMap.has(vpcComboId)) {
      comboMap.set(vpcComboId, {
        id: vpcComboId,
        type: "rect",
        label: vpcValue,
        parentId: regionComboId || undefined,
        data: { type: "vpc", value: vpcValue, region: node.region, account_id: node.account_id },
        style: COMBO_STYLE.vpc,
      });
    }

    const subnetValue = readAttribute(node, SUBNET_ATTRIBUTE_KEYS);
    const subnetComboId = subnetValue ? sanitizeId(`subnet:${subnetValue}`) : null;
    if (subnetComboId && subnetValue && !comboMap.has(subnetComboId)) {
      comboMap.set(subnetComboId, {
        id: subnetComboId,
        type: "rect",
        label: subnetValue,
        parentId: vpcComboId || regionComboId || undefined,
        data: { type: "subnet", value: subnetValue, region: node.region, account_id: node.account_id },
        style: COMBO_STYLE.subnet,
      });
    }

    const assignedCombo = subnetComboId || vpcComboId || regionComboId;
    locationAssignments[node.resource_id] = assignedCombo || regionComboId || null;
  }

  Object.entries(locationAssignments).forEach(([resourceId, comboId]) => {
    if (comboId) {
      assignments[resourceId] = comboId;
    }
  });

  return {
    combos: Array.from(comboMap.values()),
    assignments,
  };
};

const hashString = (value: string): number => {
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash << 5) - hash + value.charCodeAt(i);
    hash |= 0;
  }
  return Math.abs(hash);
};

const overlayAttributeCandidates: Record<OverlayMode, string[]> = {
  activity: ["activity_score", "invocations_last_hour", "last_activity"],
  coverage: ["coverage_score", "coverage_gap", "coverage"],
  traffic: ["traffic_score", "rps", "queue_depth", "request_per_minute"],
};

const clamp01 = (value: number): number => Math.max(0, Math.min(1, value));

const overlayValue = (node: InfraNode, overlay: OverlayMode): number => {
  const keys = overlayAttributeCandidates[overlay];
  for (const key of keys) {
    const candidate = node.attributes?.[key];
    if (typeof candidate === "number" && Number.isFinite(candidate)) {
      if (candidate > 1 && candidate < 1000) {
        return clamp01(candidate / 100);
      }
      return clamp01(candidate);
    }
  }
  return clamp01((hashString(`${overlay}:${node.resource_id}`) % 100) / 100);
};

const buildNodeVisual = (
  node: InfraNode,
  overlay: OverlayMode,
  isSelected: boolean,
): { color: string; borderColor: string; size: number; borderWidth: number } => {
  const baseColor = RESOURCE_TYPE_COLORS[node.resource_type] || "#6366f1";
  const value = overlayValue(node, overlay);
  const accent = OVERLAY_ACCENTS[overlay];
  const size = isSelected ? 22 : 12 + value * 10;
  const borderWidth = isSelected ? 4 : 1 + value * 2;
  return {
    color: baseColor,
    borderColor: accent,
    size,
    borderWidth,
  };
};

const computeSubgraph = (
  graph: { nodes: InfraNode[]; edges: InfraEdge[] } | null,
  focusId: string | null,
  depth: number,
): { nodes: InfraNode[]; edges: InfraEdge[] } => {
  if (!graph) {
    return { nodes: [], edges: [] };
  }
  if (!focusId) {
    return { nodes: graph.nodes, edges: graph.edges };
  }

  const adjacency = new Map<string, Set<string>>();
  for (const edge of graph.edges) {
    if (!adjacency.has(edge.from_resource_id)) adjacency.set(edge.from_resource_id, new Set());
    if (!adjacency.has(edge.to_resource_id)) adjacency.set(edge.to_resource_id, new Set());
    adjacency.get(edge.from_resource_id)?.add(edge.to_resource_id);
    adjacency.get(edge.to_resource_id)?.add(edge.from_resource_id);
  }

  const queue: Array<{ id: string; level: number }> = [{ id: focusId, level: 0 }];
  const included = new Set<string>([focusId]);

  while (queue.length > 0) {
    const current = queue.shift();
    if (!current) break;
    if (current.level >= depth) continue;
    const neighbors = adjacency.get(current.id);
    if (!neighbors) continue;
    for (const neighbor of neighbors) {
      if (!included.has(neighbor)) {
        included.add(neighbor);
        queue.push({ id: neighbor, level: current.level + 1 });
      }
    }
  }

  const nodes = graph.nodes.filter((node) => included.has(node.resource_id));
  const edges = graph.edges.filter(
    (edge) => included.has(edge.from_resource_id) && included.has(edge.to_resource_id),
  );

  return { nodes, edges };
};

// `watermark` is drawn over the canvas, lower right, so it is in the pixels of every screenshot (the demo)
const GraphCard: React.FC<{ title: string; loading?: boolean; watermark?: string | null; children: React.ReactNode }> = ({
  title,
  loading = false,
  watermark = null,
  children,
}) => (
  <div className="bg-white border rounded-lg shadow-sm h-[520px] flex flex-col">
    <div className="px-4 py-2 border-b font-semibold text-gray-800 flex items-center justify-between">
      <span>{title}</span>
      {loading && <span className="text-xs text-blue-500 animate-pulse">Loading…</span>}
    </div>
    <div className="relative flex-1 min-h-0">
      {children}
      {watermark && (
        <div
          data-map-watermark
          aria-hidden="true"
          className="pointer-events-none absolute bottom-3 right-4 z-10 select-none text-4xl font-black uppercase tracking-[0.3em] text-amber-600/40"
        >
          {watermark}
        </div>
      )}
    </div>
  </div>
);

const MapLegend: React.FC<{
  categoryStates: Record<string, { enabled: number; total: number }>;
  onToggleCategory: (category: string) => void;
}> = ({ categoryStates, onToggleCategory }) => (
  <div className="bg-white border rounded-lg shadow-sm p-4">
    <div className="text-sm font-semibold text-gray-800 mb-2">Legend</div>
    <div className="grid md:grid-cols-2 gap-3 text-xs text-gray-600">
      <div className="space-y-1">
        <div className="font-semibold text-gray-700">Categories</div>
        <div className="flex flex-wrap gap-2">
          {Object.entries(CATEGORY_STYLE).map(([key, style]) => {
            const state = categoryStates[key];
            const active = !state || state.enabled > 0;
            return (
              <button
                type="button"
                key={key}
                onClick={() => onToggleCategory(key)}
                className={`px-2 py-0.5 rounded-full border text-[11px] font-medium transition ${
                  active ? "opacity-100" : "opacity-40"
                }`}
                style={{
                  backgroundColor: style.fill,
                  borderColor: style.stroke,
                  color: style.accent,
                }}
              >
                {style.label}
              </button>
            );
          })}
        </div>
      </div>
      <div className="space-y-1">
        <div className="font-semibold text-gray-700">Node emphasis</div>
        <p>
          Larger nodes represent compute/storage resources (EC2, ECS, RDS). Smaller nodes depict supporting
          services (roles, configs). Hover to reveal full details; zoom in to display dense labels.
        </p>
      </div>
    </div>
  </div>
);

const GraphinViewer: React.FC<{
  data: { nodes: InfraNode[]; edges: InfraEdge[] };
  overlay: OverlayMode;
  selectedNodeId: string | null;
  selectedComboId: string | null;
  combos: GraphinCombo[];
  assignments: Record<string, string>;
  onSelectNode: (nodeId: string | null) => void;
  onSelectCombo: (comboId: string | null) => void;
  comboSpacingScale: number;
}> = ({
  data,
  overlay,
  selectedNodeId,
  selectedComboId,
  combos,
  assignments,
  onSelectNode,
  onSelectCombo,
  comboSpacingScale,
}) => {
  const [zoomLevel, setZoomLevel] = useState(1);
  const graphinData = useMemo(() => {
    const nodes = data.nodes.map((node) => {
      const isSelected = node.resource_id === selectedNodeId;
      const visuals = buildNodeVisual(node, overlay, isSelected);
      const showLabel = isSelected || ALWAYS_VISIBLE_LABEL_TYPES.has(node.resource_type) || zoomLevel >= 0.95;
      const displayName = node.name || node.resource_id.split("/").pop() || node.resource_type;
      const formattedType = node.resource_type.replace(/_/g, " ");
      return {
        id: node.resource_id,
        comboId: assignments[node.resource_id],
        data: node,
        style: {
          keyshape: {
            size: visuals.size,
            fill: visuals.color,
            stroke: isSelected ? "#f97316" : visuals.borderColor,
            lineWidth: (visuals.borderWidth || 1) + (isSelected ? 2 : 0),
          },
          label: {
            value: showLabel ? `${displayName} · ${formattedType}` : "",
            fill: isSelected ? "#111827" : "#4b5563",
          },
        },
      };
    });
    const styledCombos = combos.map((combo) => {
      const isSelected = combo.id === selectedComboId;
      const baseStyle = combo.style || {};
      const baseKeyshape = baseStyle.keyshape || {};
      return {
        ...combo,
        style: {
          ...baseStyle,
          keyshape: {
            ...baseKeyshape,
            stroke: isSelected ? "#f97316" : baseKeyshape.stroke,
            lineWidth: (baseKeyshape.lineWidth || 1) + (isSelected ? 2 : 0),
            opacity: isSelected ? 0.9 : baseKeyshape.opacity,
          },
        },
      };
    });
    const edges = data.edges.map((edge, index) => ({
      id: edge.edge_id || `graphin-edge-${index}`,
      source: edge.from_resource_id,
      target: edge.to_resource_id,
      data: edge,
      style: {
        keyshape: {
          stroke: "#cbd5f5",
          lineWidth: 1.5,
          opacity: 0.8,
        },
      },
    }));
    return { nodes, edges, combos: styledCombos };
  }, [data, overlay, selectedNodeId, selectedComboId, combos, assignments]);

  const graphinRef = useRef<Graphin | null>(null);

  useEffect(() => {
    const instance = graphinRef.current?.graph;
    if (!instance) return;

    const handleNodeClick = (event: any) => {
      const nodeId = event?.item?.getID?.();
      if (nodeId) {
        onSelectNode(nodeId);
      }
    };

    const handleComboClick = (event: any) => {
      const comboId = event?.item?.getID?.();
      if (comboId) {
        onSelectCombo(comboId);
      }
    };

    const handleCanvasClick = () => {
      onSelectNode(null);
      onSelectCombo(null);
    };

    const handleViewport = () => {
      setZoomLevel(instance.getZoom?.() ?? 1);
    };
    handleViewport();

    instance.on("node:click", handleNodeClick);
    instance.on("combo:click", handleComboClick);
    instance.on("canvas:click", handleCanvasClick);
    instance.on("viewportchange", handleViewport);

    return () => {
      instance.off("node:click", handleNodeClick);
      instance.off("combo:click", handleComboClick);
      instance.off("canvas:click", handleCanvasClick);
      instance.off("viewportchange", handleViewport);
    };
  }, [graphinData, onSelectNode, onSelectCombo]);

  useEffect(() => {
    const graph = graphinRef.current?.graph;
    if (!graph) return;

    const enforceComboBounds = (event: any) => {
      const item = event?.item;
      if (!item) return;
      const model = item.getModel?.();
      if (!model) return;
      const comboId = model.comboId;
      if (!comboId) return;
      const comboBBox = (graph as any)?.getComboBBox?.(comboId);
      if (!comboBBox) return;
      let { x = 0, y = 0 } = model;
      const padding = 24;
      const minX = comboBBox.minX + padding;
      const maxX = comboBBox.maxX - padding;
      const minY = comboBBox.minY + padding;
      const maxY = comboBBox.maxY - padding;

      let updated = false;
      if (x < minX) {
        x = minX;
        updated = true;
      } else if (x > maxX) {
        x = maxX;
        updated = true;
      }
      if (y < minY) {
        y = minY;
        updated = true;
      } else if (y > maxY) {
        y = maxY;
        updated = true;
      }
      if (updated) {
        graph.updateItem(item, { x, y });
      }
    };

    graph.on("node:drag", enforceComboBounds);
    graph.on("node:dragend", enforceComboBounds);

    return () => {
      graph.off("node:drag", enforceComboBounds);
      graph.off("node:dragend", enforceComboBounds);
    };
  }, [graphinData]);

  useEffect(() => {
    const graph = graphinRef.current?.graph;
    if (!graph || combos.length === 0) return;

    const ensureEntry = (
      store: Map<string, { combos: string[]; nodes: string[] }>,
      id: string,
    ): { combos: string[]; nodes: string[] } => {
      if (!store.has(id)) {
        store.set(id, { combos: [], nodes: [] });
      }
      return store.get(id)!;
    };

    const buildHierarchy = () => {
      const mapping = new Map<string, { combos: string[]; nodes: string[] }>();
      combos.forEach((combo) => {
        ensureEntry(mapping, combo.id);
        if (combo.parentId) {
          ensureEntry(mapping, combo.parentId).combos.push(combo.id);
        }
      });
      Object.entries(assignments).forEach(([nodeId, comboId]) => {
        if (!comboId) return;
        ensureEntry(mapping, comboId).nodes.push(nodeId);
      });
      return mapping;
    };

    const hierarchy = buildHierarchy();
    const nodeCache = new Map<string, string[]>();

    const getNodePosition = (nodeId: string): { x: number; y: number } | null => {
      const item = graph.findById(nodeId);
      if (!item) return null;
      const model = item.getModel?.();
      if (!model) return null;
      return { x: model.x ?? 0, y: model.y ?? 0 };
    };

    const computeCentroid = (nodeIds: string[]): { x: number; y: number } | null => {
      if (nodeIds.length === 0) return null;
      let sumX = 0;
      let sumY = 0;
      let count = 0;
      nodeIds.forEach((nodeId) => {
        const pos = getNodePosition(nodeId);
        if (!pos) return;
        sumX += pos.x;
        sumY += pos.y;
        count += 1;
      });
      if (count === 0) return null;
      return { x: sumX / count, y: sumY / count };
    };

    const collectNodes = (comboId: string): string[] => {
      if (nodeCache.has(comboId)) return nodeCache.get(comboId)!;
      const entry = hierarchy.get(comboId);
      if (!entry) {
        nodeCache.set(comboId, []);
        return [];
      }
      const nodes: string[] = [...entry.nodes];
      entry.combos.forEach((childId) => {
        nodes.push(...collectNodes(childId));
      });
      nodeCache.set(comboId, nodes);
      return nodes;
    };

    const shiftComboNodes = (comboId: string, dx: number, dy: number) => {
      if (!dx && !dy) return;
      const nodeIds = collectNodes(comboId);
      if (nodeIds.length === 0) return;
      nodeIds.forEach((nodeId) => {
        const item = graph.findById(nodeId);
        if (!item) return;
        const model = item.getModel?.();
        const nextX = (model?.x || 0) + dx;
        const nextY = (model?.y || 0) + dy;
        graph.updateItem(item, { x: nextX, y: nextY });
      });
    };

    const separateCombos = () => {
      const combosByBucket = combos.reduce<Record<string, GraphinCombo[]>>((acc, combo) => {
        const typeKey = resolveComboType(combo);
        const parentKey = combo.parentId || "__root__";
        const bucketKey = `${typeKey}::${parentKey}`;
        if (!acc[bucketKey]) acc[bucketKey] = [];
        acc[bucketKey].push(combo);
        return acc;
      }, {});
      const getBBox = (comboId: string) => ((graph as any).getComboBBox?.(comboId) || null) as ComboBBox | null;

      // getComboBBox walks every child of the combo, so calling it inside the
      // pairwise loop made this O(combos^2) bbox rebuilds per iteration — the
      // single most expensive thing on the maps tab. Snapshot the bboxes once
      // per iteration instead and translate them alongside the nodes: combos in
      // the same bucket share a parent and never share nodes, so a shift is
      // exactly a translation of the bbox.
      let bboxes = new Map<string, ComboBBox | null>();
      const translateBBox = (comboId: string, dx: number, dy: number) => {
        const bbox = bboxes.get(comboId);
        if (!bbox) return;
        bboxes.set(comboId, {
          ...bbox,
          minX: bbox.minX + dx,
          maxX: bbox.maxX + dx,
          minY: bbox.minY + dy,
          maxY: bbox.maxY + dy,
        });
      };
      const shiftCombo = (comboId: string, dx: number, dy: number) => {
        shiftComboNodes(comboId, dx, dy);
        translateBBox(comboId, dx, dy);
      };

      // Every shiftComboNodes call updates nodes one at a time; without this
      // each update repaints the whole canvas.
      const autoPaint = (graph as any).get?.("autoPaint") ?? true;
      graph.setAutoPaint?.(false);

      for (let iteration = 0; iteration < 10; iteration += 1) {
        let movedThisRound = false;
        bboxes = new Map(combos.map((combo) => [combo.id, getBBox(combo.id)]));
        Object.values(combosByBucket).forEach((comboList) => {
          if (comboList.length < 2) return;
          for (let i = 0; i < comboList.length; i += 1) {
            for (let j = i + 1; j < comboList.length; j += 1) {
              const comboA = comboList[i];
              const comboB = comboList[j];
              const bboxA = bboxes.get(comboA.id);
              const bboxB = bboxes.get(comboB.id);
              if (!bboxA || !bboxB) continue;
              const padding = comboPairPadding(comboA, comboB, comboSpacingScale);
              const separation = computeComboSeparation(bboxA, bboxB, padding);
              if (!separation) continue;
              const halfDx = separation.dx / 2;
              const halfDy = separation.dy / 2;
              shiftCombo(comboA.id, -halfDx, -halfDy);
              shiftCombo(comboB.id, halfDx, halfDy);
              movedThisRound = true;
            }
          }
        });
        if (!movedThisRound) break;
        if (typeof (graph as any).updateCombos === "function") {
          (graph as any).updateCombos();
        } else if (typeof graph.refreshPositions === "function") {
          graph.refreshPositions();
        }
      }

      graph.setAutoPaint?.(autoPaint);
      if (typeof graph.paint === "function") {
        graph.paint();
      }
    };

    const handler = () => {
      separateCombos();
    };
    graph.on("afterlayout", handler);
    handler();
    return () => {
      graph.off("afterlayout", handler);
    };
  }, [graphinData, combos, assignments, comboSpacingScale]);

  return (
    <Graphin
      ref={graphinRef as any}
      data={graphinData}
      fitView
      layout={{
        type: "force2",
        // Animating the force settling repaints ~500 nodes every tick and keeps
        // the combo-separation pass waiting on it. Solving the layout up front
        // gets to a usable graph far sooner.
        animation: false,
        maxIteration: 200,
        nodeStrength: 360,
        edgeStrength: 220,
        preventOverlap: true,
        nodeSpacing: 60,
        comboSpacing: 260,
        gravity: 0,
        linkDistance: 220,
      }}
    >
      <Behaviors.ZoomCanvas />
      <Behaviors.DragCanvas />
      <Behaviors.DragNode />
      <GraphinComponents.Tooltip hasArrow>
        {(model) => {
          const node = model?.model?.data as InfraNode | undefined;
          if (!node) return null;
          return (
            <div className="text-xs text-gray-800 space-y-1">
              <div className="font-semibold">{node.name || node.resource_id}</div>
              <div className="text-gray-500 capitalize">{node.resource_type.replace(/_/g, " ")}</div>
              <div className="text-gray-500">Region: {node.region}</div>
              {node.account_id && <div className="text-gray-500">Account: {node.account_id}</div>}
            </div>
          );
        }}
      </GraphinComponents.Tooltip>
    </Graphin>
  );
};

const relationshipLabel = (edge: InfraEdge): string =>
  `${edge.relationship_type} → ${edge.to_resource_id.split(":").pop() || edge.to_resource_id}`;

const MapsTab: React.FC<MapsTabProps> = ({ infraScan = null, mapperJob = null, onMapperTriggered }) => {
  const { settingsBootstrap, searchParams } = useShell();
  const demo = isDemoApp(settingsBootstrap?.app);
  const wantedSearch = searchParams?.get("q") ?? null;
  const [mapData, setMapData] = useState<InfraMapResponse | null>(null);
  const [mapLoading, setMapLoading] = useState(false);
  const [mapError, setMapError] = useState<string | null>(null);
  const [mapperJobId, setMapperJobId] = useState<string | null>(null);
  const [mapperJobStatus, setMapperJobStatus] = useState<string | null>(null);
  const [mapperError, setMapperError] = useState<string | null>(null);
  const [overlay, setOverlay] = useState<OverlayMode>("activity");
  const [depth, setDepth] = useState(6);
  const [focusMode, setFocusMode] = useState(false);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedComboId, setSelectedComboId] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState("");
  const [orgInput, setOrgInput] = useState("");
  const [resourceFilters, setResourceFilters] = useState<ResourceFilterMap>({});
  const [comboSpacingScale, setComboSpacingScale] = useState(1);
  const effectiveMapperStatus = mapperJob?.status || mapperJobStatus;
  const effectiveMapperError = mapperError || mapperJob?.error || infraScan?.last_error || null;
  const mapperBusy = effectiveMapperStatus === "queued" || effectiveMapperStatus === "running" || effectiveMapperStatus === "retrying";
  const latestMapperEvent = mapperJob?.events?.length ? mapperJob.events[mapperJob.events.length - 1] : null;
  const latestMapperMessage = latestMapperEvent?.message || infraScan?.latest_message || null;

  const loadMap = useCallback(async () => {
    setMapLoading(true);
    setMapError(null);
    try {
      const response = await axios.get<InfraMapResponse>("/api/maps", {
        params: {
          org_id: orgInput || undefined,
          limit: 4000,
        },
      });
      setMapData(response.data);
    } catch (error: any) {
      const message = error?.response?.data?.detail || error?.message || "Failed to load infra map";
      setMapError(message);
    } finally {
      setMapLoading(false);
    }
  }, [orgInput]);

  const triggerMapper = useCallback(async () => {
    setMapperError(null);
    try {
      const response = await axios.post<MapperRunResponse>("/api/maps/reindex", {
        mode: "full",
        org_id: orgInput || undefined,
      });
      setMapperJobId(response.data.job_id);
      setMapperJobStatus(response.data.status);
      onMapperTriggered?.();
    } catch (error: any) {
      const message = error?.response?.data?.detail || error?.message || "Failed to start mapper";
      setMapperError(message);
    }
  }, [onMapperTriggered, orgInput]);

  // The graph payload is ~850 KB. Mount, `scanned` and `last_completed_at` used
  // to drive three separate effects, so opening the tab fired three identical
  // requests back to back. Reload only when the scan signature actually changes.
  const loadedSignatureRef = useRef<string | null>(null);
  useEffect(() => {
    const signature = `${orgInput}|${infraScan?.scanned === true}|${infraScan?.last_completed_at || ""}`;
    if (loadedSignatureRef.current === signature) return;
    loadedSignatureRef.current = signature;
    void loadMap();
  }, [orgInput, infraScan?.scanned, infraScan?.last_completed_at, loadMap]);

  useEffect(() => {
    if (!mapData) return;
    setResourceFilters((prev) => {
      const typeSet = new Set(mapData.nodes.map((node) => node.resource_type));
      const next: ResourceFilterMap = {};
      let changed = false;
      typeSet.forEach((type) => {
        if (!(type in prev)) changed = true;
        next[type] = type in prev ? prev[type] : true;
      });
      Object.keys(prev).forEach((type) => {
        if (!typeSet.has(type)) changed = true;
      });
      if (!changed) return prev;
      return next;
    });
  }, [mapData]);

  useEffect(() => {
    if (!mapperJobId) return;
    let cancelled = false;
    const interval = window.setInterval(async () => {
      try {
        const response = await axios.get<JobStatusResponse>(`/api/jobs/${encodeURIComponent(mapperJobId)}`);
        if (cancelled) return;
        const status = response.data.status;
        setMapperJobStatus(status);
        if (status === "completed") {
          window.clearInterval(interval);
          await loadMap();
          onMapperTriggered?.();
        } else if (status === "failed" || status === "cancelled") {
          window.clearInterval(interval);
          setMapperError(response.data.error || `Mapper job ${status}`);
          onMapperTriggered?.();
        }
      } catch (error: any) {
        if (cancelled) return;
        window.clearInterval(interval);
        setMapperError(error?.response?.data?.detail || error?.message || "Failed to read mapper job status");
      }
    }, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [loadMap, mapperJobId, onMapperTriggered]);

const enabledTypes = useMemo(() => {
    const entries = Object.entries(resourceFilters);
    if (entries.length === 0) return null;
    const active = entries.filter(([, enabled]) => enabled).map(([type]) => type);
    return new Set(active);
  }, [resourceFilters]);

  const typeFilteredGraph = useMemo(() => {
    if (!mapData) return { nodes: [] as InfraNode[], edges: [] as InfraEdge[] };
    const typeSet =
      enabledTypes ?? new Set(mapData.nodes.map((node) => node.resource_type));
    const nodes = mapData.nodes.filter((node) => typeSet.has(node.resource_type));
    const nodeIds = new Set(nodes.map((node) => node.resource_id));
    const edges = mapData.edges.filter(
      (edge) => nodeIds.has(edge.from_resource_id) && nodeIds.has(edge.to_resource_id),
    );
    return { nodes, edges };
  }, [mapData, enabledTypes]);

  const filteredGraph = useMemo(
    () => computeSubgraph(typeFilteredGraph, focusMode && selectedNodeId ? selectedNodeId : null, depth),
    [typeFilteredGraph, focusMode, selectedNodeId, depth],
  );

  const comboData = useMemo(() => buildGraphinCombos(filteredGraph.nodes), [filteredGraph.nodes]);

  const categoryTypesMap = useMemo(() => {
    const map: Record<string, string[]> = {};
    Object.entries(RESOURCE_TYPE_CATEGORIES).forEach(([type, category]) => {
      if (!map[category]) map[category] = [];
      map[category].push(type);
    });
    return map;
  }, []);

  const categoryStates = useMemo(() => {
    const state: Record<string, { enabled: number; total: number }> = {};
    Object.entries(categoryTypesMap).forEach(([category, types]) => {
      const total = types.length;
      const enabled = types.filter((type) => resourceFilters[type] !== false).length;
      state[category] = { enabled, total };
    });
    return state;
  }, [categoryTypesMap, resourceFilters]);

const toggleResourceType = useCallback((type: string) => {
  setResourceFilters((prev) => ({
    ...prev,
    [type]: !prev[type],
  }));
}, []);

const setAllResourceFilters = useCallback((value: boolean) => {
  setResourceFilters((prev) => {
    if (Object.keys(prev).length === 0) return prev;
    const next: ResourceFilterMap = {};
    Object.keys(prev).forEach((type) => {
      next[type] = value;
    });
    return next;
  });
}, []);

const toggleCategoryFilter = useCallback(
  (category: string) => {
    setResourceFilters((prev) => {
      const next: ResourceFilterMap = { ...prev };
      const types = categoryTypesMap[category] || [];
      if (types.length === 0) {
        return prev;
      }
      const shouldEnable = types.some((type) => next[type] === false);
      types.forEach((type) => {
        next[type] = shouldEnable;
      });
      return next;
    });
  },
  [categoryTypesMap],
);

  useEffect(() => {
    if (selectedNodeId && !filteredGraph.nodes.some((node) => node.resource_id === selectedNodeId)) {
      setSelectedNodeId(null);
    }
  }, [filteredGraph.nodes, selectedNodeId]);

  useEffect(() => {
    if (selectedComboId && !comboData.combos.some((combo) => combo.id === selectedComboId)) {
      setSelectedComboId(null);
    }
  }, [comboData.combos, selectedComboId]);

  const selectedNode = useMemo(() => {
    if (!selectedNodeId) return null;
    return filteredGraph.nodes.find((node) => node.resource_id === selectedNodeId) || null;
  }, [filteredGraph.nodes, selectedNodeId]);

  const selectedCombo = useMemo(() => {
    if (!selectedComboId) return null;
    return comboData.combos.find((combo) => combo.id === selectedComboId) || null;
  }, [comboData.combos, selectedComboId]);

  const comboChildrenMap = useMemo(() => {
    const map: Record<string, string[]> = {};
    comboData.combos.forEach((combo) => {
      if (!combo.parentId) return;
      if (!map[combo.parentId]) map[combo.parentId] = [];
      map[combo.parentId].push(combo.id);
    });
    return map;
  }, [comboData.combos]);

  const selectedComboNodes = useMemo(() => {
    if (!selectedCombo) return [] as InfraNode[];
    const targetCombos = new Set<string>();
    const stack = [selectedCombo.id];
    while (stack.length) {
      const current = stack.pop();
      if (!current || targetCombos.has(current)) continue;
      targetCombos.add(current);
      (comboChildrenMap[current] || []).forEach((child) => stack.push(child));
    }
    return filteredGraph.nodes.filter((node) => {
      const assigned = comboData.assignments[node.resource_id];
      return assigned ? targetCombos.has(assigned) : false;
    });
  }, [selectedCombo, comboChildrenMap, filteredGraph.nodes, comboData.assignments]);

  const typeOptions = useMemo(
    () => Object.keys(resourceFilters).sort((a, b) => a.localeCompare(b)),
    [resourceFilters],
  );

  const relatedEdges = useMemo(() => {
    if (!selectedNodeId) {
      return { incoming: [] as InfraEdge[], outgoing: [] as InfraEdge[] };
    }
    return {
      incoming: filteredGraph.edges.filter((edge) => edge.to_resource_id === selectedNodeId),
      outgoing: filteredGraph.edges.filter((edge) => edge.from_resource_id === selectedNodeId),
    };
  }, [filteredGraph.edges, selectedNodeId]);

  const handleSearch = useCallback(() => {
    if (!mapData) return;
    const term = searchTerm.trim().toLowerCase();
    if (!term) return;
    const hit = mapData.nodes.find(
      (node) =>
        node.name?.toLowerCase().includes(term) || node.resource_id.toLowerCase().includes(term),
    );
    if (hit) {
      setSelectedNodeId(hit.resource_id);
    }
  }, [mapData, searchTerm]);

  // ?q=<term> (the tour's "search payments"): the box filled and the first hit selected, once the map is here
  const [searchedFromUrl, setSearchedFromUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!wantedSearch || !mapData || searchedFromUrl === wantedSearch) return;
    setSearchedFromUrl(wantedSearch);
    setSearchTerm(wantedSearch);
    const term = wantedSearch.trim().toLowerCase();
    const hit = mapData.nodes.find((node) => node.name?.toLowerCase().includes(term) || node.resource_id.toLowerCase().includes(term));
    if (hit) setSelectedNodeId(hit.resource_id);
  }, [wantedSearch, mapData, searchedFromUrl]);
  // the tour's first step: a node's detail opened
  useEffect(() => {
    if (demo && selectedNodeId) completeTourStep("map");
  }, [demo, selectedNodeId]);

  return (
    <section className="space-y-6">
      <div className="bg-white border rounded-lg shadow-sm p-4 space-y-4">
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-600">Org</label>
            <input
              type="text"
              className="border rounded px-2 py-1 text-sm"
              value={orgInput}
              placeholder="Default from target"
              onChange={(event) => setOrgInput(event.target.value)}
            />
            <button
              className="px-3 py-1 rounded bg-blue-600 text-white text-sm"
              onClick={loadMap}
              disabled={mapLoading}
            >
              {mapLoading ? "Loading…" : "Load"}
            </button>
          </div>
          <div className="flex items-center gap-4">
            <label className="flex items-center gap-2 text-sm text-gray-700">
              <input
                type="checkbox"
                className="rounded border-gray-300"
                checked={focusMode}
                onChange={(event) => setFocusMode(event.target.checked)}
              />
              <span>Focus on selection</span>
            </label>
            <div className={`flex items-center gap-2 ${focusMode ? "" : "opacity-40"}`}>
              <span className="text-sm text-gray-600">Depth</span>
              <input
                type="range"
                min={1}
                max={6}
                value={depth}
                disabled={!focusMode}
                onChange={(event) => setDepth(Number(event.target.value))}
              />
              <span className="font-semibold text-sm">{depth}</span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {(["activity", "coverage", "traffic"] as OverlayMode[]).map((mode) => (
              <button
                key={mode}
                onClick={() => setOverlay(mode)}
                className={`px-3 py-1 rounded-full text-xs font-semibold border ${
                  overlay === mode
                    ? "bg-gray-900 text-white border-gray-900"
                    : "bg-white text-gray-600 border-gray-300"
                }`}
              >
                {mode.charAt(0).toUpperCase() + mode.slice(1)}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-3">
            <label className="text-sm text-gray-600 whitespace-nowrap">Container spacing</label>
            <input
              type="range"
              min={0.6}
              max={1.8}
              step={0.1}
              value={comboSpacingScale}
              onChange={(event) => setComboSpacingScale(Number(event.target.value))}
            />
            <span className="text-sm font-semibold text-gray-700">{comboSpacingScale.toFixed(1)}×</span>
          </div>
          {typeOptions.length > 0 && (
            <div className="flex flex-col gap-2 w-full">
              <div className="flex items-center justify-between text-xs text-gray-600">
                <span className="font-semibold text-gray-700 uppercase tracking-wide">
                  Resource types
                </span>
                <div className="flex items-center gap-2">
                  <button
                    className="px-2 py-0.5 rounded border border-gray-300 text-gray-700 hover:bg-gray-100"
                    onClick={() => setAllResourceFilters(true)}
                  >
                    All
                  </button>
                  <button
                    className="px-2 py-0.5 rounded border border-gray-300 text-gray-700 hover:bg-gray-100"
                    onClick={() => setAllResourceFilters(false)}
                  >
                    None
                  </button>
                </div>
              </div>
              <div className="flex flex-wrap gap-3">
                {typeOptions.map((type) => (
                  <label
                    key={type}
                    className={`flex items-center gap-1 text-xs font-medium ${
                      resourceFilters[type] ? "text-gray-800" : "text-gray-400"
                    }`}
                  >
                    <input
                      type="checkbox"
                      className="rounded border-gray-300"
                      checked={resourceFilters[type]}
                      onChange={() => toggleResourceType(type)}
                    />
                    {type}
                  </label>
                ))}
              </div>
            </div>
          )}
          <div className="flex items-center gap-2 flex-1">
            <input
              type="text"
              className="border rounded px-3 py-1 text-sm w-full"
              placeholder="Search node name or ARN"
              value={searchTerm}
              onChange={(event) => setSearchTerm(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  handleSearch();
                }
              }}
            />
            <button
              className="px-3 py-1 rounded bg-gray-200 text-gray-800 text-sm"
              onClick={handleSearch}
            >
              Focus
            </button>
            <button
              className="px-3 py-1 rounded bg-gray-100 text-gray-600 text-sm"
              onClick={() => {
                setSelectedNodeId(null);
                setSelectedComboId(null);
              }}
            >
              Clear
            </button>
          </div>
        </div>
        {infraScan?.integrations_ready && (
          <div
            className={`rounded-lg border px-4 py-3 text-sm ${
              mapperBusy
                ? "border-sky-200 bg-sky-50 text-sky-900"
                : infraScan?.scanned
                ? "border-emerald-200 bg-emerald-50 text-emerald-900"
                : effectiveMapperStatus === "failed" || infraScan?.status === "failed"
                  ? "border-red-200 bg-red-50 text-red-900"
                  : "border-sky-200 bg-sky-50 text-sky-900"
            }`}
          >
            <div className="flex flex-wrap items-center gap-3">
              <span className="font-semibold">
                {/* `scanned` is reset while any scan runs (it gates features on a current map); a scan
                    that has completed before makes this one a re-scan */}
                {mapperBusy && (infraScan?.scanned || infraScan?.last_completed_at)
                  ? "Re-scan in progress"
                  : !infraScan?.scanned
                  ? mapperBusy
                    ? "Full infra scan in progress"
                    : infraScan?.status === "failed"
                      ? "Full infra scan failed"
                      : "Full infra scan required"
                  : "Infra scan complete"}
              </span>
              {effectiveMapperStatus && (
                <span className="rounded-full bg-white/70 px-2 py-0.5 text-xs font-medium uppercase tracking-wide">
                  {effectiveMapperStatus}
                </span>
              )}
              {infraScan?.last_completed_at && (
                <span className="text-xs opacity-80">
                  Last completed: {formatIsoDateTime(infraScan.last_completed_at)}
                </span>
              )}
            </div>
            {mapperBusy && mapperJob && <MappingProgressBar job={mapperJob} />}
            {(latestMapperMessage || infraScan?.last_error || infraScan?.disabled_reason) && (
              <div className="mt-2 text-xs opacity-85">
                {latestMapperMessage || infraScan?.last_error || infraScan?.disabled_reason}
              </div>
            )}
          </div>
        )}
        <div className="flex flex-wrap items-center gap-6 text-sm text-gray-600">
          <span>
            Nodes:{" "}
            <strong className="text-gray-900">{mapData?.nodes.length ?? 0}</strong>
          </span>
          <span>
            Edges:{" "}
            <strong className="text-gray-900">{mapData?.edges.length ?? 0}</strong>
          </span>
          <button
            className="px-3 py-1 rounded border border-gray-300 text-gray-700 hover:bg-gray-100 disabled:opacity-60"
            onClick={loadMap}
            disabled={mapLoading}
          >
            {mapLoading ? "Refreshing…" : "Refresh map"}
          </button>
          <button
            className="px-3 py-1 rounded bg-gray-900 text-white hover:bg-gray-800 disabled:opacity-60"
            onClick={triggerMapper}
            disabled={mapperBusy}
          >
            {mapperBusy ? "Mapping…" : "Run mapping"}
          </button>
          {effectiveMapperStatus && (
            <span className="text-gray-700">
              Mapper job: <strong className="text-gray-900">{effectiveMapperStatus}</strong>
            </span>
          )}
          {mapError && <span className="text-red-600">{mapError}</span>}
          {effectiveMapperError && <span className="text-red-600">{effectiveMapperError}</span>}
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-1 gap-4">
        <GraphCard title="AntV Graphin" loading={mapLoading} watermark={demo ? "demo" : null}>
          {filteredGraph.nodes.length === 0 ? (
            <div className="h-full flex items-center justify-center text-sm text-gray-500">
              {mapLoading ? "Loading map…" : "No graph data available"}
            </div>
          ) : (
            <GraphinViewer
              data={filteredGraph}
              overlay={overlay}
              selectedNodeId={selectedNodeId}
              selectedComboId={selectedComboId}
              combos={comboData.combos}
              assignments={comboData.assignments}
              comboSpacingScale={comboSpacingScale}
              onSelectNode={(nodeId) => {
                setSelectedNodeId(nodeId);
                if (nodeId) {
                  setSelectedComboId(null);
                }
              }}
              onSelectCombo={(comboId) => {
                setSelectedComboId(comboId);
                if (comboId) {
                  setSelectedNodeId(null);
                }
              }}
            />
          )}
        </GraphCard>
      </div>

      <MapLegend categoryStates={categoryStates} onToggleCategory={toggleCategoryFilter} />

      <div className="bg-white border rounded-lg shadow-sm p-4">
        {selectedNode ? (
          <div className="space-y-3">
            <div>
              <h3 className="text-lg font-semibold text-gray-900 flex items-center gap-3">
                Node details
                <span className="text-xs uppercase tracking-wide text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full">
                  {selectedNode.resource_type}
                </span>
              </h3>
              <p className="text-sm text-gray-600">{selectedNode.name}</p>
              {selectedNode.attributes?.synthetic === true && (
                <p className="text-xs font-medium text-amber-700">Synthetic resource (demo world)</p>
              )}
            </div>
            <div className="grid md:grid-cols-2 gap-4 text-sm">
              <div className="space-y-1">
                <div className="text-gray-500 uppercase text-[11px] font-semibold">Resource ID</div>
                <div className="font-mono text-xs break-all">{selectedNode.resource_id}</div>
              </div>
              <div className="space-y-1">
                <div className="text-gray-500 uppercase text-[11px] font-semibold">Region / Account</div>
                <div className="font-mono text-xs">
                  {selectedNode.region} · {selectedNode.account_id}
                </div>
              </div>
            </div>
            {Object.keys(selectedNode.tags || {}).length > 0 && (
              <div className="space-y-1">
                <div className="text-gray-500 uppercase text-[11px] font-semibold">Tags</div>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(selectedNode.tags).map(([key, value]) => (
                    <span key={key} className="text-xs bg-gray-100 text-gray-700 rounded px-2 py-0.5">
                      {key}: {value}
                    </span>
                  ))}
                </div>
              </div>
            )}
            {Object.keys(selectedNode.attributes || {}).length > 0 && (
              <div className="space-y-1">
                <div className="text-gray-500 uppercase text-[11px] font-semibold">Attributes</div>
                <div className="grid md:grid-cols-2 gap-2 text-xs bg-gray-50 rounded-lg p-3">
                  {Object.entries(selectedNode.attributes).map(([key, value]) => (
                    <div key={key} className="flex flex-col">
                      <span className="text-[10px] uppercase text-gray-500">{key}</span>
                      <span className="font-mono text-[11px] text-gray-800 break-all">
                        {typeof value === "string" ? value : JSON.stringify(value)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
            <div className="grid md:grid-cols-2 gap-4">
              <div>
                <h4 className="text-sm font-semibold text-gray-800 mb-2">Outgoing</h4>
                {relatedEdges.outgoing.length === 0 ? (
                  <p className="text-xs text-gray-500">No outgoing relationships</p>
                ) : (
                  <ul className="text-xs space-y-1">
                    {relatedEdges.outgoing.map((edge) => (
                      <li key={edge.edge_id || `${edge.from_resource_id}-${edge.to_resource_id}`}>
                        <button
                          className="text-blue-600 hover:underline"
                          onClick={() => {
                            setSelectedNodeId(edge.to_resource_id);
                            setSelectedComboId(null);
                          }}
                        >
                          {edge.relationship_type} → {edge.to_resource_id.split(":").pop()}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <div>
                <h4 className="text-sm font-semibold text-gray-800 mb-2">Incoming</h4>
                {relatedEdges.incoming.length === 0 ? (
                  <p className="text-xs text-gray-500">No incoming relationships</p>
                ) : (
                  <ul className="text-xs space-y-1">
                    {relatedEdges.incoming.map((edge) => (
                      <li key={edge.edge_id || `${edge.from_resource_id}-${edge.to_resource_id}`}>
                        <button
                          className="text-blue-600 hover:underline"
                          onClick={() => {
                            setSelectedNodeId(edge.from_resource_id);
                            setSelectedComboId(null);
                          }}
                        >
                          {edge.from_resource_id.split(":").pop()} → {edge.relationship_type}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          </div>
        ) : selectedCombo ? (
          <div className="space-y-4">
            <div>
              <h3 className="text-lg font-semibold text-gray-900 flex items-center gap-3">
                Combo details
                <span className="text-xs uppercase tracking-wide text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full">
                  {selectedCombo.data?.type || "container"}
                </span>
              </h3>
              <p className="text-sm text-gray-600">{selectedCombo.label}</p>
            </div>
            <div className="grid md:grid-cols-3 gap-4 text-sm">
              {selectedCombo.data?.region && (
                <div>
                  <div className="text-gray-500 uppercase text-[11px] font-semibold">Region</div>
                  <div className="font-mono text-xs">{selectedCombo.data.region}</div>
                </div>
              )}
              {selectedCombo.data?.account_id && (
                <div>
                  <div className="text-gray-500 uppercase text-[11px] font-semibold">Account</div>
                  <div className="font-mono text-xs">{selectedCombo.data.account_id}</div>
                </div>
              )}
              <div>
                <div className="text-gray-500 uppercase text-[11px] font-semibold">Resources</div>
                <div className="font-semibold text-gray-900">{selectedComboNodes.length}</div>
              </div>
            </div>
            <div className="space-y-2">
              <div className="flex items-center justify-between text-xs text-gray-500">
                <span>Nodes inside container</span>
              </div>
              {selectedComboNodes.length === 0 ? (
                <p className="text-xs text-gray-500">No nodes currently mapped inside this combo.</p>
              ) : (
                <ul className="max-h-48 overflow-auto text-xs space-y-1">
                  {selectedComboNodes.map((node) => (
                    <li key={node.resource_id}>
                      <button
                        className="text-blue-600 hover:underline"
                        onClick={() => {
                          setSelectedNodeId(node.resource_id);
                          setSelectedComboId(null);
                        }}
                      >
                        {node.name || node.resource_type}
                      </button>
                      <span className="text-gray-400"> · {node.resource_type}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        ) : (
          <div className="text-sm text-gray-500">Select a node or combo to see details.</div>
        )}
      </div>
    </section>
  );
};

export default MapsTab;
