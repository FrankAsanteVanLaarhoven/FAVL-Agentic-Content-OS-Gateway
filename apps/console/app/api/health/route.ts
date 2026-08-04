import { NextResponse } from "next/server";
import { promQuery } from "@/lib/gateway";
import type { Readiness } from "@/lib/types";

export const dynamic = "force-dynamic";

/**
 * Platform health.
 *
 * Readiness is read directly rather than through APISIX: /health/* is the
 * unauthenticated route, and if identity itself is down we still want the
 * status bar to report on the data plane instead of going blank.
 */
async function readiness(base: string): Promise<Readiness | null> {
  try {
    const response = await fetch(`${base}/readyz`, {
      cache: "no-store",
      signal: AbortSignal.timeout(3000),
    });
    // 503 is a real answer, not a failure: the body says which dependency.
    return (await response.json()) as Readiness;
  } catch {
    return null;
  }
}

export async function GET() {
  const orchestratorUrl =
    process.env.ORCHESTRATOR_URL ?? "http://orchestrator:8000";
  const registryUrl =
    process.env.REGISTRY_URL ?? "http://connector-registry:8001";

  const [orchestrator, registry, invocations, failures, pending, dead] =
    await Promise.all([
      readiness(orchestratorUrl),
      readiness(registryUrl),
      // `or vector(0)` distinguishes two states the UI must not conflate:
      // a counter that has not been incremented since the process started
      // (genuinely zero) from Prometheus being unreachable (unknown, "—").
      // Without it a healthy but idle service looks like a metrics outage.
      promQuery("sum(favl_connector_invocations_total) or vector(0)"),
      promQuery(
        'sum(favl_connector_invocations_total{status=~"failed_.*|timed_out"}) or vector(0)',
      ),
      promQuery("sum(favl_outbox_pending) or vector(0)"),
      promQuery("sum(favl_outbox_dead) or vector(0)"),
    ]);

  const bothUp = Boolean(orchestrator && registry);
  const bothReady =
    orchestrator?.status === "ready" && registry?.status === "ready";

  return NextResponse.json({
    gateway: bothReady ? "ok" : bothUp ? "degraded" : "down",
    orchestrator,
    registry,
    metrics: { invocations, failures, pending, dead },
  });
}
