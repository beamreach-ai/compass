/** Human output for tool results: tables for arrays of flat objects, key/value otherwise. */

const MAX_COL = 100;

function cell(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") {
    const json = JSON.stringify(value);
    return json.length > MAX_COL ? json.slice(0, MAX_COL - 1) + "…" : json;
  }
  const text = String(value);
  // URLs, ARNs and ids are copied by the user: never cut them.
  if (/^(https?:\/\/|arn:aws:|cmp_|tok_|[a-z]+-[0-9a-f]{8,})/.test(text)) return text;
  return text.length > MAX_COL ? text.slice(0, MAX_COL - 1) + "…" : text;
}

export function table(rows: Record<string, unknown>[], columns?: string[]): string {
  if (!rows.length) return "(none)";
  const cols = columns || Array.from(rows.reduce((set, row) => {
    Object.keys(row).forEach((k) => set.add(k));
    return set;
  }, new Set<string>()));
  const widths = cols.map((c) => Math.max(c.length, ...rows.map((r) => cell(r[c]).length)));
  const line = (values: string[]) => values.map((v, i) => v.padEnd(widths[i])).join("  ").trimEnd();
  const out = [line(cols), line(widths.map((w) => "-".repeat(w)))];
  for (const row of rows) out.push(line(cols.map((c) => cell(row[c]))));
  return out.join("\n");
}

export function keyValue(obj: Record<string, unknown>, indent = ""): string {
  const lines: string[] = [];
  for (const [key, value] of Object.entries(obj)) {
    if (value !== null && typeof value === "object" && !Array.isArray(value)) {
      lines.push(`${indent}${key}:`);
      lines.push(keyValue(value as Record<string, unknown>, indent + "  "));
    } else if (Array.isArray(value) && value.length && typeof value[0] === "object" && value[0] !== null) {
      lines.push(`${indent}${key}:`);
      lines.push(table(value as Record<string, unknown>[]).split("\n").map((l) => indent + "  " + l).join("\n"));
    } else {
      lines.push(`${indent}${key}: ${Array.isArray(value) ? value.join(", ") : value === null || value === undefined ? "" : typeof value === "object" ? cell(value) : String(value)}`);
    }
  }
  return lines.join("\n");
}

export function renderResult(result: unknown): string {
  if (result === null || result === undefined) return "ok";
  if (typeof result === "string") return result;
  if (Array.isArray(result)) {
    if (!result.length) return "(none)";
    if (typeof result[0] === "object" && result[0] !== null) return table(result as Record<string, unknown>[]);
    return result.map(String).join("\n");
  }
  if (typeof result === "object") {
    const obj = result as Record<string, unknown>;
    if (obj.executed === false && obj.plan && typeof obj.plan === "object") {
      const p = obj.plan as { summary: string; steps: string[]; warnings?: string[]; [key: string]: unknown };
      const lines = [`PLAN: ${p.summary}`, ...p.steps.map((s, i) => `  ${i + 1}. ${s}`)];
      const cost = p.cost as { monthly_usd: number; lines: Array<{ item: string; monthly_usd: number; note?: string }>; disclaimer?: string } | undefined;
      if (cost?.lines) {
        lines.push("", `Estimated monthly cost: about $${cost.monthly_usd}`);
        for (const l of cost.lines) lines.push(`  $${String(l.monthly_usd).padStart(6)}  ${l.item}${l.note ? ` (${l.note})` : ""}`);
        if (cost.disclaimer) lines.push(`  ${cost.disclaimer}`);
      }
      const extras = Object.fromEntries(Object.entries(p).filter(([k]) => !["summary", "steps", "warnings", "cost", "vpc", "prereqs"].includes(k)));
      if (Object.keys(extras).length) lines.push("", keyValue(extras));
      if (p.warnings?.length) lines.push("", ...p.warnings.map((w) => `  ! ${w}`));
      lines.push("", "Nothing was executed. Re-run with --yes (or the confirmation flag the plan names) to apply.");
      return lines.join("\n");
    }
    return keyValue(obj);
  }
  return String(result);
}
