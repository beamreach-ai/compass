/** Thin HTTP client for the Compass engine API. Maps engine error shapes to typed errors. */

import {
  ApiError,
  AuthError,
  CaptainUnavailableError,
  InfraScanPendingError,
  LimitReachedError,
  NotConfiguredError,
  ScopeError,
} from "./errors.js";
import type { Connection } from "./config.js";

export interface RequestOptions {
  params?: Record<string, string | number | boolean | undefined | null>;
  body?: unknown;
  /** multipart upload: field name -> {filename, content, contentType} */
  files?: Record<string, { filename: string; content: Uint8Array | string; contentType?: string }>;
  headers?: Record<string, string>;
  timeoutMs?: number;
  /** return raw response instead of parsed JSON */
  raw?: boolean;
}

export class CompassClient {
  readonly baseUrl: string;
  private readonly token?: string;
  private readonly workspaceId?: string;
  private readonly fetchImpl: typeof fetch;

  constructor(connection: Connection, fetchImpl: typeof fetch = fetch) {
    this.baseUrl = connection.url.replace(/\/+$/, "");
    this.token = connection.token;
    this.workspaceId = connection.workspace_id;
    this.fetchImpl = fetchImpl;
  }

  static fromConnection(connection: Connection | null): CompassClient {
    if (!connection) throw new NotConfiguredError();
    return new CompassClient(connection);
  }

  url(path: string, params?: RequestOptions["params"]): string {
    const url = new URL(path.startsWith("/") ? path : `/${path}`, this.baseUrl + "/");
    if (params) {
      for (const [key, value] of Object.entries(params)) {
        if (value === undefined || value === null || value === "") continue;
        url.searchParams.set(key, String(value));
      }
    }
    return url.toString();
  }

  private headers(extra?: Record<string, string>, hasJsonBody = false): Record<string, string> {
    const headers: Record<string, string> = { accept: "application/json", ...(extra || {}) };
    if (hasJsonBody) headers["content-type"] = "application/json";
    if (this.token) headers["authorization"] = `Bearer ${this.token}`;
    if (this.workspaceId) headers["x-workspace-id"] = this.workspaceId;
    return headers;
  }

  async request<T = any>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? 60_000);
    let body: BodyInit | undefined;
    let jsonBody = false;
    if (options.files) {
      const form = new FormData();
      for (const [field, file] of Object.entries(options.files)) {
        const blob = new Blob([file.content as any], { type: file.contentType || "application/octet-stream" });
        form.append(field, blob, file.filename);
      }
      if (options.body && typeof options.body === "object") {
        for (const [key, value] of Object.entries(options.body as Record<string, unknown>)) {
          if (value !== undefined && value !== null) form.append(key, String(value));
        }
      }
      body = form;
    } else if (options.body !== undefined) {
      body = JSON.stringify(options.body);
      jsonBody = true;
    }

    let response: Response;
    try {
      response = await this.fetchImpl(this.url(path, options.params), {
        method,
        headers: this.headers(options.headers, jsonBody),
        body,
        signal: controller.signal,
        redirect: "manual",
      });
    } catch (err: any) {
      clearTimeout(timer);
      const reason = err?.name === "AbortError" ? "timed out" : err?.message || String(err);
      throw new ApiError(0, `Could not reach Compass at ${this.baseUrl}: ${reason}`);
    }
    clearTimeout(timer);

    if (options.raw) return response as unknown as T;

    const text = await response.text();
    let data: any = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = text;
      }
    }

    if (response.ok) return data as T;
    const detail = typeof data === "object" && data && "detail" in data ? String((data as any).detail) : String(data || response.statusText);
    const code = data && typeof data === "object" ? (data as any).code : undefined;
    if (typeof code === "string" && (code.startsWith("captain_") || code === "dependency_off")) {
      throw new CaptainUnavailableError(detail, data);
    }
    switch (response.status) {
      case 401:
        throw new AuthError(detail);
      case 403:
        if (data && typeof data === "object" && "required_scope" in data) {
          throw new ScopeError(detail, data);
        }
        throw new ApiError(403, detail, data);
      case 402:
        throw new LimitReachedError(detail, data);
      case 409:
        if (/infra scan|infrastructure scan/i.test(detail)) throw new InfraScanPendingError(detail);
        throw new ApiError(409, detail, data);
      default:
        throw new ApiError(response.status, `${method} ${path} failed (${response.status}): ${detail}`, data);
    }
  }

  get<T = any>(path: string, params?: RequestOptions["params"]) {
    return this.request<T>("GET", path, { params });
  }
  post<T = any>(path: string, body?: unknown, params?: RequestOptions["params"]) {
    return this.request<T>("POST", path, { body, params });
  }
  put<T = any>(path: string, body?: unknown) {
    return this.request<T>("PUT", path, { body });
  }
  patch<T = any>(path: string, body?: unknown) {
    return this.request<T>("PATCH", path, { body });
  }
  delete<T = any>(path: string, params?: RequestOptions["params"]) {
    return this.request<T>("DELETE", path, { params });
  }
}
