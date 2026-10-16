/** Derive commander flags from a zod raw shape, and parse flag values back into schema values. */

import { z } from "zod";

export interface FlagSpec {
  key: string;
  flag: string; // e.g. --finding-ids <value>
  kind: "string" | "number" | "boolean" | "enum" | "string[]" | "json";
  choices?: string[];
  description: string;
  required: boolean;
}

export function kebab(key: string): string {
  return key.replace(/_/g, "-");
}

export function camel(key: string): string {
  return key.replace(/[-_]([a-z0-9])/g, (_m, c: string) => c.toUpperCase());
}

export function unwrap(schema: z.ZodTypeAny): { inner: z.ZodTypeAny; optional: boolean } {
  let inner = schema;
  let optional = false;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const def: any = inner._def;
    if (def?.typeName === "ZodOptional" || def?.typeName === "ZodNullable") {
      optional = true;
      inner = def.innerType;
    } else if (def?.typeName === "ZodDefault") {
      optional = true;
      inner = def.innerType;
    } else if (def?.typeName === "ZodEffects") {
      inner = def.schema;
    } else {
      break;
    }
  }
  return { inner, optional };
}

export function flagSpecs(shape: z.ZodRawShape, skip: string[] = []): FlagSpec[] {
  const specs: FlagSpec[] = [];
  for (const [key, schema] of Object.entries(shape)) {
    if (skip.includes(key)) continue;
    const { inner, optional } = unwrap(schema as z.ZodTypeAny);
    const def: any = inner._def;
    const description = (schema as z.ZodTypeAny).description || inner.description || "";
    const name = kebab(key);
    let spec: FlagSpec;
    switch (def.typeName) {
      case "ZodBoolean":
        spec = { key, flag: `--${name} [true|false]`, kind: "boolean", description, required: false };
        break;
      case "ZodNumber":
        spec = { key, flag: `--${name} <number>`, kind: "number", description, required: !optional };
        break;
      case "ZodEnum":
        spec = { key, flag: `--${name} <value>`, kind: "enum", choices: def.values, description: `${description} (${def.values.join("|")})`.trim(), required: !optional };
        break;
      case "ZodArray": {
        const item = unwrap(def.type).inner._def;
        if (item.typeName === "ZodObject") {
          spec = { key, flag: `--${name} <json>`, kind: "json", description: `${description} (JSON array)`.trim(), required: !optional };
        } else {
          spec = { key, flag: `--${name} <a,b,c>`, kind: "string[]", choices: item.typeName === "ZodEnum" ? item.values : undefined, description, required: !optional };
        }
        break;
      }
      case "ZodObject":
        spec = { key, flag: `--${name} <json>`, kind: "json", description: `${description} (JSON)`.trim(), required: !optional };
        break;
      default:
        spec = { key, flag: `--${name} <value>`, kind: "string", description, required: !optional };
    }
    specs.push(spec);
  }
  return specs;
}

export function coerce(spec: FlagSpec, raw: unknown): unknown {
  if (raw === undefined || raw === null) return undefined;
  switch (spec.kind) {
    case "boolean":
      if (raw === true || raw === "" || raw === undefined) return true;
      return !["false", "0", "no", "off"].includes(String(raw).toLowerCase());
    case "number":
      return typeof raw === "number" ? raw : Number(raw);
    case "string[]":
      if (Array.isArray(raw)) return raw.flatMap((v) => String(v).split(",")).map((s) => s.trim()).filter(Boolean);
      return String(raw).split(",").map((s) => s.trim()).filter(Boolean);
    case "json":
      return typeof raw === "string" ? JSON.parse(raw) : raw;
    case "enum": {
      const value = Array.isArray(raw) ? raw.join(" ") : String(raw);
      if (spec.choices?.includes(value)) return value;
      const snake = value.replace(/-/g, "_");
      return spec.choices?.includes(snake) ? snake : value;
    }
    default:
      return Array.isArray(raw) ? raw.join(" ") : String(raw);
  }
}
