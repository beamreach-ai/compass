// Demo mode (docs/demo.md): what the bootstrap's `app.demo` block says and the words the
// dashboard uses for it. `app.mode === "demo"` is the switch; the block is only there then.

export type DemoConfig = {
  links?: { install?: string | null; repository?: string | null; slack?: string | null } | null;
  pinned_settings?: string[] | null;
  world?: { resources?: number; seed?: number } | null;
  cooldown_seconds?: number | null;
  // the signed-in user is one of demo.admins (or an admin token): the only one who sees Settings
  admin?: boolean | null;
};

export const isDemoApp = (app?: { mode?: string | null } | null): boolean => app?.mode === "demo";

/** A settings section the demo fixes (github, aws, demo, notifications.endpoints). */
export const isPinned = (demo: DemoConfig | null | undefined, key: string): boolean =>
  Array.isArray(demo?.pinned_settings) && demo!.pinned_settings!.includes(key);

export const PINNED_TITLE = "Fixed in the demo";
/** Props for a control the demo pins: disabled with the reason as its title. */
export const pinnedProps = (pinned: boolean, disabled = false) => (pinned ? { disabled: true, title: PINNED_TITLE } : { disabled });

/** Who did a write, as the demo shows it: never a name, never an email. */
export const attribution = (userId: string | null | undefined, me: string | null | undefined): string | null => {
  if (!userId) return null;
  return me && userId === me ? "by you" : "by a visitor";
};

export const DEMO_BANNER_TEXT =
  "Demo account. Plant, open pull requests, trip a decoy and watch the response: it all runs against a shared demo account that resets on Sunday.";
export const DEMO_BANNER_DISMISSED_KEY = "compass:demo-banner-dismissed";
