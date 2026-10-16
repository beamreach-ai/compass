/** A fake Compass engine: route table -> JSON responses, with request capture. */

export interface Captured {
  method: string;
  path: string;
  query: Record<string, string>;
  body: unknown;
  headers: Record<string, string>;
}

type Handler = (req: Captured) => unknown | { status: number; body: unknown };

export function fakeEngine(routes: Record<string, Handler | unknown>) {
  const calls: Captured[] = [];
  const fetchImpl: typeof fetch = async (input, init) => {
    const url = new URL(typeof input === "string" ? input : (input as URL).toString());
    const method = (init?.method || "GET").toUpperCase();
    const headers: Record<string, string> = {};
    for (const [k, v] of Object.entries((init?.headers as Record<string, string>) || {})) headers[k.toLowerCase()] = v;
    let body: unknown = undefined;
    if (typeof init?.body === "string") {
      try {
        body = JSON.parse(init.body);
      } catch {
        body = init.body;
      }
    } else if (init?.body) {
      body = init.body;
    }
    const query: Record<string, string> = {};
    url.searchParams.forEach((v, k) => (query[k] = v));
    const req: Captured = { method, path: url.pathname, query, body, headers };
    calls.push(req);
    const key = `${method} ${url.pathname}`;
    const handler = routes[key] ?? routes[`* ${url.pathname}`];
    if (handler === undefined) {
      return new Response(JSON.stringify({ detail: `no route ${key}` }), { status: 404, headers: { "content-type": "application/json" } });
    }
    const result = typeof handler === "function" ? (handler as Handler)(req) : handler;
    if (result && typeof result === "object" && "status" in (result as any) && "body" in (result as any)) {
      const r = result as { status: number; body: unknown };
      return new Response(JSON.stringify(r.body), { status: r.status, headers: { "content-type": "application/json" } });
    }
    return new Response(JSON.stringify(result), { status: 200, headers: { "content-type": "application/json" } });
  };
  return { fetchImpl, calls };
}

export const connection = { url: "http://compass.test", token: "cmp_test-token", workspace_id: "ws-test" };
