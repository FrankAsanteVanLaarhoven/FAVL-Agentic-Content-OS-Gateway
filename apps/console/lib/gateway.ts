import "server-only";

/**
 * Server-side gateway client.
 *
 * The console never holds a token in the browser. Every call is made from a
 * route handler through APISIX, so the console exercises the same
 * authenticated path an external client would — including OIDC verification
 * and rate limiting — rather than reaching the services directly and proving
 * nothing about the gateway.
 */

const GATEWAY = process.env.GATEWAY_URL ?? "http://apisix:9080";
const TOKEN_URL =
  process.env.KEYCLOAK_TOKEN_URL ??
  "http://keycloak:8080/realms/favl/protocol/openid-connect/token";

type CachedToken = { value: string; expiresAt: number };
let cached: CachedToken | null = null;
let inFlight: Promise<string> | null = null;

async function fetchToken(): Promise<string> {
  const body = new URLSearchParams({
    grant_type: "password",
    client_id: process.env.KEYCLOAK_CLIENT_ID ?? "agentic-content-os",
    client_secret: process.env.KEYCLOAK_CLIENT_SECRET ?? "",
    username: process.env.CONSOLE_USERNAME ?? "demo",
    password: process.env.CONSOLE_PASSWORD ?? "",
  });

  const response = await fetch(TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
    cache: "no-store",
  });

  if (!response.ok) {
    throw new GatewayError(
      response.status,
      `identity provider rejected the console credentials`,
    );
  }

  const json = (await response.json()) as {
    access_token: string;
    expires_in: number;
  };
  // Renew a minute early so a request never races the expiry.
  cached = {
    value: json.access_token,
    expiresAt: Date.now() + (json.expires_in - 60) * 1000,
  };
  return cached.value;
}

async function token(): Promise<string> {
  if (cached && cached.expiresAt > Date.now()) return cached.value;
  // Collapse concurrent renewals so a burst of requests triggers one grant.
  inFlight ??= fetchToken().finally(() => {
    inFlight = null;
  });
  return inFlight;
}

export class GatewayError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly detail?: unknown,
  ) {
    super(message);
  }
}

export async function gateway<T>(
  path: string,
  init: RequestInit & { retryOn401?: boolean } = {},
): Promise<T> {
  const { retryOn401 = true, ...rest } = init;
  const response = await fetch(`${GATEWAY}${path}`, {
    ...rest,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      Authorization: `Bearer ${await token()}`,
      "X-Actor-ID": process.env.CONSOLE_USERNAME ?? "console",
      ...(rest.headers ?? {}),
    },
    cache: "no-store",
  });

  if (response.status === 401 && retryOn401) {
    // The cached token was revoked or the realm restarted; one clean retry.
    cached = null;
    return gateway<T>(path, { ...init, retryOn401: false });
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const parsed = text ? safeJson(text) : undefined;

  if (!response.ok) {
    throw new GatewayError(
      response.status,
      `gateway responded ${response.status}`,
      parsed ?? text,
    );
  }
  return parsed as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

/** Prometheus instant query, used for the operational counters. */
export async function promQuery(query: string): Promise<number | null> {
  const base = process.env.PROMETHEUS_URL ?? "http://prometheus:9090";
  try {
    const response = await fetch(
      `${base}/api/v1/query?query=${encodeURIComponent(query)}`,
      { cache: "no-store" },
    );
    if (!response.ok) return null;
    const json = (await response.json()) as {
      data?: { result?: { value?: [number, string] }[] };
    };
    const results = json.data?.result ?? [];
    if (results.length === 0) return null;
    return results.reduce(
      (sum, r) => sum + Number.parseFloat(r.value?.[1] ?? "0"),
      0,
    );
  } catch {
    // A metrics outage must not take the console down; the tile shows "—".
    return null;
  }
}
