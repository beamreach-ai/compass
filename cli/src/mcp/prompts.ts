/** Skills exposed as MCP prompts so non-Claude-Code clients get the same playbooks. */

import { promises as fs, readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

function skillsDir(): string {
  const here = path.dirname(new URL(import.meta.url).pathname);
  return path.resolve(here, "..", "..", "plugin", "skills");
}

function stripFrontmatter(markdown: string): { description: string; body: string } {
  const match = markdown.match(/^---\n([\s\S]*?)\n---\n?([\s\S]*)$/);
  if (!match) return { description: "", body: markdown };
  const description = (match[1].match(/^description:\s*(.*)$/m) || [])[1] || "";
  return { description: description.trim(), body: match[2] };
}

export function registerPrompts(server: McpServer, extraDirs: string[] = []): void {
  // Registered lazily at connect time is not supported; read synchronously via a small cache instead.
  const seen = new Set<string>();
  for (const dir of [skillsDir(), ...extraDirs]) registerDir(server, dir, seen);
}

function registerDir(server: McpServer, dir: string, seen: Set<string>): void {
  let entries: string[] = [];
  try {
    entries = readdirSync(dir);
  } catch {
    return;
  }
  for (const name of entries) {
    if (seen.has(name)) continue;
    seen.add(name);
    const file = path.join(dir, name, "SKILL.md");
    let raw: string;
    try {
      raw = readFileSync(file, "utf8");
    } catch {
      continue;
    }
    const { description, body } = stripFrontmatter(raw);
    server.registerPrompt(
      `compass_${name.replace(/^compass-/, "").replace(/-/g, "_")}`,
      { title: name, description: description || `Compass playbook: ${name}` },
      () => ({ messages: [{ role: "user", content: { type: "text", text: body } }] }),
    );
  }
}

export async function listSkills(): Promise<string[]> {
  try {
    return await fs.readdir(skillsDir());
  } catch {
    return [];
  }
}
