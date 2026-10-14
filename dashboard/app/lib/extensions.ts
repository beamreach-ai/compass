// Where the product adds to the open dashboard without the shell importing it.
//
// This is the open edition's copy, shipped to the public repository as
// `extensions.ts`: every registry is empty, so DashboardClient renders the open
// tabs and settings sections and nothing else. The private repository's
// `extensions.ts` has the same exports, filled from `app/product/`. The API does
// the same in compass_api/extensions.py.
import type React from "react";
import type { DashboardTab } from "./tabs";

// A tab the product adds: `key` is its URL/tab key, `feature` the flag in
// `disabled_features` that hides it (the API's register_tabs mapping).
export type ProductTab = {
  key: DashboardTab;
  label: string;
  feature: string;
  Component: React.ComponentType;
};

// Where a product section sits among the open Settings sections:
//   top                 - before AWS access
//   after_integrations  - after AWS access, GitHub and Slack, before Pull requests
//   bottom              - after Pull requests, before the "Save settings" bar
export type SettingsSlot = "top" | "after_integrations" | "bottom";

export type ProductSettingsSection = {
  key: string;
  slot: SettingsSlot;
  Component: React.ComponentType;
};

// Wrapped around the shell body in order, so product state that spans tabs
// (findings, billing) lives for the whole session, as it did in one component.
export type ProductProvider = React.ComponentType<{ children: React.ReactNode }>;

export const productProviders: ProductProvider[] = [];
export const productTabs: ProductTab[] = [];
export const productSettingsSections: ProductSettingsSection[] = [];
// The header banners, rendered after the read-only notice and before the infra-scan banner.
export const productBanners: React.ComponentType[] = [];
