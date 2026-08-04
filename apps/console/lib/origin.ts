import "server-only";

/**
 * Same-origin enforcement for any future state-changing route handler.
 *
 * The console's route handlers hold a gateway credential. Without an origin
 * check, a cross-site form POST would execute with that credential — the
 * classic confused deputy. `Sec-Fetch-Site` is set by the browser and cannot
 * be forged by page script; the Origin comparison is the fallback for clients
 * that do not send it.
 */
export function requireSameOrigin(request: Request): void {
  const site = request.headers.get("sec-fetch-site");
  if (site && site !== "same-origin") {
    throw new CrossOriginRequest(`Sec-Fetch-Site: ${site}`);
  }

  const origin = request.headers.get("origin");
  if (origin) {
    const host = request.headers.get("host");
    if (!host || new URL(origin).host !== host) {
      throw new CrossOriginRequest(`Origin ${origin} does not match ${host}`);
    }
  } else if (!site) {
    // Neither signal present: refuse rather than assume same-origin.
    throw new CrossOriginRequest("no Origin or Sec-Fetch-Site header");
  }
}

export class CrossOriginRequest extends Error {}
