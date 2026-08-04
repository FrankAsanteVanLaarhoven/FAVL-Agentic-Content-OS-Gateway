"use client";

import { useQuery } from "@tanstack/react-query";
import { StatusDot } from "@/components/primitives";
import type { Readiness, Tone } from "@/lib/types";

type HealthPayload = {
  gateway: "ok" | "degraded" | "down";
  orchestrator: Readiness | null;
  registry: Readiness | null;
  metrics: {
    invocations: number | null;
    failures: number | null;
    pending: number | null;
    dead: number | null;
  };
};

function serviceTone(readiness: Readiness | null): Tone {
  if (!readiness) return "err";
  return readiness.status === "ready" ? "ok" : "warn";
}

export function StatusBar() {
  const { data, isError } = useQuery<HealthPayload>({
    queryKey: ["health"],
    queryFn: async () => {
      const response = await fetch("/api/health");
      if (!response.ok) throw new Error("health unavailable");
      return response.json();
    },
    refetchInterval: 5000,
  });

  const dead = data?.metrics.dead ?? 0;
  const pending = data?.metrics.pending ?? 0;

  return (
    <footer className="flex h-7 shrink-0 items-center gap-4 border-t border-[var(--color-line)] bg-[var(--color-surface)] px-3 text-[11px] text-[var(--color-muted)]">
      {isError ? (
        <StatusDot tone="err" label="console cannot reach the platform" />
      ) : (
        <>
          <StatusDot
            tone={serviceTone(data?.orchestrator ?? null)}
            label="orchestrator"
          />
          <StatusDot
            tone={serviceTone(data?.registry ?? null)}
            label="registry"
          />
          <span className="text-[var(--color-line-strong)]">│</span>
          <StatusDot
            tone={data?.registry?.nats_connected ? "ok" : "err"}
            label="jetstream"
          />
          <span className="tabular">
            outbox{" "}
            <span
              className={
                pending > 0
                  ? "text-[var(--color-warn)]"
                  : "text-[var(--color-ink)]"
              }
            >
              {pending}
            </span>{" "}
            pending
          </span>
          {dead > 0 && (
            // A dead letter is a committed change with no delivered event.
            // It is never folded into a generic "warnings" count.
            <StatusDot tone="err" label={`${dead} dead-lettered`} />
          )}
          <span className="ml-auto flex items-center gap-4">
            <span className="tabular">
              {data?.metrics.invocations ?? "—"} invocations
            </span>
            <span className="text-[var(--color-faint)]">
              local development · dev realm
            </span>
          </span>
        </>
      )}
    </footer>
  );
}
