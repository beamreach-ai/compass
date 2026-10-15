"use client";
// A settings form drawn from a captain's config schema (JSON Schema with
// x-order, x-group, x-help and x-secret), read and written through
// /api/captains/{name}/config. Strings, numbers, booleans, enums, string lists
// and one level of nested object; anything richer is a captain's own component.
import axios from "axios";
import React, { useCallback, useEffect, useMemo, useState } from "react";

type Schema = {
  type?: string | string[];
  title?: string;
  properties?: Record<string, Schema>;
  items?: Schema;
  enum?: Array<string | number>;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  "x-order"?: number;
  "x-group"?: string;
  "x-help"?: string;
  "x-secret"?: boolean;
};

type Values = Record<string, unknown>;

const typeOf = (schema: Schema): string => (Array.isArray(schema.type) ? schema.type[0] : schema.type) || "string";

const orderedFields = (schema: Schema): Array<[string, Schema]> =>
  Object.entries(schema.properties || {}).sort(([, a], [, b]) => (a["x-order"] ?? 1000) - (b["x-order"] ?? 1000));

const labelOf = (key: string): string => {
  const words = key.replace(/[-_]+/g, " ");
  return words ? words[0].toUpperCase() + words.slice(1) : key;
};

function Field({ name, schema, value, onChange, disabled }: { name: string; schema: Schema; value: unknown; onChange: (v: unknown) => void; disabled: boolean }) {
  const kind = typeOf(schema);
  const help = schema["x-help"];
  const id = `schema-field-${name}`;
  const base = "w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-gray-900 focus:outline-none disabled:bg-gray-50 disabled:text-gray-500";

  if (kind === "boolean") {
    return (
      <label className="flex items-start gap-3 text-sm">
        <input type="checkbox" className="mt-1" checked={value === true} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
        <span>
          <span className="font-medium text-gray-900">{labelOf(name)}</span>
          {help && <span className="block text-xs text-gray-500">{help}</span>}
        </span>
      </label>
    );
  }

  let control: React.ReactNode;
  if (schema.enum) {
    control = (
      <select id={id} className={base} value={value === undefined || value === null ? "" : String(value)} disabled={disabled} onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}>
        <option value="">—</option>
        {schema.enum.map((option) => (
          <option key={String(option)} value={String(option)}>{String(option)}</option>
        ))}
      </select>
    );
  } else if (kind === "integer" || kind === "number") {
    control = (
      <input
        id={id}
        type="number"
        className={base}
        value={value === undefined || value === null ? "" : String(value)}
        min={schema.minimum}
        max={schema.maximum}
        step={kind === "integer" ? 1 : "any"}
        disabled={disabled}
        onChange={(e) => {
          const raw = e.target.value;
          if (raw === "") return onChange(null);
          const parsed = kind === "integer" ? parseInt(raw, 10) : parseFloat(raw);
          onChange(Number.isFinite(parsed) ? parsed : null);
        }}
      />
    );
  } else if (kind === "array") {
    const list = Array.isArray(value) ? (value as unknown[]).map(String) : [];
    control = (
      <input
        id={id}
        type="text"
        className={base}
        value={list.join(", ")}
        placeholder="comma-separated"
        disabled={disabled}
        onChange={(e) => {
          const items = e.target.value.split(",").map((s) => s.trim()).filter(Boolean);
          onChange(items.length ? items : null);
        }}
      />
    );
  } else {
    control = (
      <input
        id={id}
        type={schema["x-secret"] ? "password" : "text"}
        autoComplete={schema["x-secret"] ? "new-password" : undefined}
        className={base}
        value={value === undefined || value === null ? "" : String(value)}
        placeholder={schema["x-secret"] ? "unchanged" : undefined}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
      />
    );
  }

  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium text-gray-900">
        {labelOf(name)}
        {schema["x-secret"] && <span className="ml-2 text-[10px] font-semibold uppercase tracking-wide text-gray-400">secret</span>}
      </label>
      {control}
      {help && <p className="text-xs text-gray-500">{help}</p>}
    </div>
  );
}

function Fields({ schema, values, onChange, disabled, prefix = "" }: { schema: Schema; values: Values; onChange: (next: Values) => void; disabled: boolean; prefix?: string }) {
  const groups = useMemo(() => {
    const byGroup = new Map<string, Array<[string, Schema]>>();
    for (const entry of orderedFields(schema)) {
      const group = entry[1]["x-group"] || "";
      if (!byGroup.has(group)) byGroup.set(group, []);
      byGroup.get(group)!.push(entry);
    }
    return Array.from(byGroup.entries());
  }, [schema]);

  const set = (key: string, value: unknown) => onChange({ ...values, [key]: value });

  return (
    <div className="space-y-5">
      {groups.map(([group, entries]) => (
        <fieldset key={group || "_"} className="space-y-4">
          {group && <legend className="text-xs font-semibold uppercase tracking-wide text-gray-500">{group}</legend>}
          {entries.map(([key, sub]) =>
            typeOf(sub) === "object" && sub.properties ? (
              <div key={key} className="rounded border border-gray-200 p-4">
                <div className="mb-3 text-sm font-semibold text-gray-900">{sub.title || labelOf(key)}</div>
                {sub["x-help"] && <p className="mb-3 text-xs text-gray-500">{sub["x-help"]}</p>}
                <Fields
                  schema={sub}
                  values={(values[key] as Values) || {}}
                  onChange={(next) => set(key, next)}
                  disabled={disabled}
                  prefix={`${prefix}${key}.`}
                />
              </div>
            ) : (
              <Field key={key} name={`${prefix}${key}`} schema={sub} value={values[key]} onChange={(v) => set(key, v)} disabled={disabled} />
            ),
          )}
        </fieldset>
      ))}
    </div>
  );
}

export default function SchemaForm({ captain, readOnly, onSaved }: { captain: string; readOnly: boolean; onSaved?: () => void }) {
  const [schema, setSchema] = useState<Schema | null>(null);
  const [values, setValues] = useState<Values>({});
  const [loaded, setLoaded] = useState<Values>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await axios.get(`/api/captains/${encodeURIComponent(captain)}/config`);
      setSchema(res.data?.schema || null);
      const config = (res.data?.config && typeof res.data.config === "object" ? res.data.config : {}) as Values;
      setValues(config);
      setLoaded(config);
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || "Failed to load configuration");
    } finally {
      setLoading(false);
    }
  }, [captain]);

  useEffect(() => {
    void load();
  }, [load]);

  const dirty = JSON.stringify(values) !== JSON.stringify(loaded);

  const save = async () => {
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const res = await axios.put(`/api/captains/${encodeURIComponent(captain)}/config`, values);
      const config = (res.data?.config || {}) as Values;
      setValues(config);
      setLoaded(config);
      setSuccess("Saved.");
      onSaved?.();
    } catch (e: any) {
      const body = e?.response?.data;
      setError((typeof body?.detail === "string" && body.detail) || e?.message || "Failed to save configuration");
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="text-sm text-gray-500">Loading configuration…</div>;
  if (!schema) return <div className="text-sm text-gray-500">{error || "This captain has no settings of its own."}</div>;

  return (
    <div className="space-y-4">
      <Fields schema={schema} values={values} onChange={setValues} disabled={readOnly || saving} />
      {error && <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      {success && !dirty && <div className="text-sm text-green-700">{success}</div>}
      {!readOnly && (
        <div className="flex items-center justify-end gap-3">
          <button type="button" onClick={() => setValues(loaded)} disabled={!dirty || saving} className="rounded border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50">
            Reset
          </button>
          <button type="button" onClick={save} disabled={!dirty || saving} className="rounded bg-gray-900 px-4 py-1.5 text-sm font-semibold text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50">
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      )}
    </div>
  );
}
