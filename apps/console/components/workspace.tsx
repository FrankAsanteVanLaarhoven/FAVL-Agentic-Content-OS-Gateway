"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { ArrowUpRight } from "lucide-react";
import {
  ClientClock,
  Empty,
  Panel,
  Pill,
  Stat,
  StatusDot,
} from "@/components/primitives";
import { BLOCKED_ITEMS } from "@/lib/nav";
import {
  connectorTone,
  invocationTone,
  type Agent,
  type Connector,
  type Invocation,
  type Readiness,
} from "@/lib/types";

type HealthPayload = {
  orchestrator: Readiness | null;
  registry: Readiness | null;
  metrics: {
    invocations: number | null;
    failures: number | null;
    pending: number | null;
    dead: number | null;
  };
};

async function get<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

export function Workspace() {
  const health = useQuery<HealthPayload>({
    queryKey: ["health"],
    queryFn: () => get("/api/health"),
    refetchInterval: 5000,
  });
  const agents = useQuery<Agent[]>({
    queryKey: ["agents"],
    queryFn: () => get("/api/agents"),
  });
  const connectors = useQuery<Connector[]>({
    queryKey: ["connectors"],
    queryFn: () => get("/api/connectors"),
  });
  const invocations = useQuery<Invocation[]>({
    queryKey: ["invocations", "recent"],
    queryFn: () => get("/api/invocations?limit=12"),
    refetchInterval: 4000,
  });

  const metrics = health.data?.metrics;
  const failureRate =
    metrics?.invocations && metrics.invocations > 0
      ? ((metrics.failures ?? 0) / metrics.invocations) * 100
      : null;

  return (
    <div className="grid h-full grid-rows-[auto_minmax(0,1fr)] gap-3 overflow-auto p-3">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Panel className="col-span-2 lg:col-span-1">
          <Stat
            label="Invocations"
            value={metrics?.invocations ?? null}
            tone="idle"
          />
        </Panel>
        <Panel>
          <Stat
            label="Failure rate"
            value={failureRate === null ? null : `${failureRate.toFixed(1)}%`}
            tone={
              failureRate === null ? "idle" : failureRate > 5 ? "warn" : "ok"
            }
            hint="Share of invocations ending failed or timed out"
          />
        </Panel>
        <Panel>
          <Stat
            label="Outbox pending"
            value={metrics?.pending ?? null}
            tone={(metrics?.pending ?? 0) > 0 ? "warn" : "ok"}
          />
        </Panel>
        <Panel>
          <Stat
            label="Dead-lettered"
            value={metrics?.dead ?? null}
            tone={(metrics?.dead ?? 0) > 0 ? "err" : "ok"}
            hint="Committed changes with no delivered event"
          />
        </Panel>
        <Panel>
          <Stat
            label="Adapters"
            value={health.data?.registry?.adapters?.length ?? null}
            tone="idle"
            hint={health.data?.registry?.adapters?.join(", ")}
          />
        </Panel>
      </div>

      <div className="grid min-h-0 grid-cols-1 gap-3 xl:grid-cols-[1fr_360px]">
        <Panel
          title="Recent invocations"
          action={
            <Link
              href="/observability"
              className="flex items-center gap-1 text-[11px] text-[var(--color-muted)] transition-colors hover:text-[var(--color-ink)]"
            >
              Event console <ArrowUpRight className="size-3" />
            </Link>
          }
        >
          {invocations.data?.length ? (
            <table className="w-full text-xs">
              <tbody>
                {invocations.data.map((row) => (
                  <tr
                    key={row.id}
                    className="border-b border-[var(--color-line)] last:border-0"
                  >
                    <td className="w-20 px-3 py-1.5 font-mono text-[11px] text-[var(--color-faint)]">
                      <ClientClock iso={row.completed_at ?? row.started_at} />
                    </td>
                    <td className="px-1 py-1.5">
                      <StatusDot
                        tone={invocationTone(row.status)}
                        label={row.status}
                      />
                    </td>
                    <td className="px-3 py-1.5 text-[var(--color-muted)]">
                      {row.adapter_kind}
                    </td>
                    <td className="truncate px-3 py-1.5 text-[var(--color-ink)]">
                      {row.operation || "—"}
                    </td>
                    <td className="tabular w-16 px-3 py-1.5 text-right text-[var(--color-muted)]">
                      {row.duration_ms === null
                        ? "—"
                        : `${Math.round(row.duration_ms)}ms`}
                    </td>
                    <td className="w-40 truncate px-3 py-1.5 text-right">
                      {row.error_code ? (
                        <span className="font-mono text-[10px] text-[var(--color-err)]">
                          {row.error_code}
                        </span>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty
              title="No invocations yet"
              body="Invoke a connector and it will appear here within a few seconds."
            />
          )}
        </Panel>

        <div className="grid min-h-0 grid-rows-2 gap-3">
          <Panel title={`Connectors · ${connectors.data?.length ?? 0}`}>
            {connectors.data?.length ? (
              <ul>
                {connectors.data.slice(0, 8).map((connector) => (
                  <li
                    key={connector.id}
                    className="flex items-center gap-2 border-b border-[var(--color-line)] px-3 py-1.5 last:border-0"
                  >
                    <StatusDot
                      tone={connectorTone(connector.status)}
                      label={connector.status}
                      className="shrink-0"
                    />
                    <span className="min-w-0 flex-1 truncate text-xs">
                      {connector.name}
                    </span>
                    <Pill>{connector.kind}</Pill>
                  </li>
                ))}
              </ul>
            ) : (
              <Empty title="No connectors registered" />
            )}
          </Panel>

          <Panel title={`Agents · ${agents.data?.length ?? 0}`}>
            {agents.data?.length ? (
              <ul>
                {agents.data.slice(0, 8).map((agent) => (
                  <li
                    key={agent.id}
                    className="flex items-center gap-2 border-b border-[var(--color-line)] px-3 py-1.5 last:border-0"
                  >
                    <span className="min-w-0 flex-1 truncate text-xs">
                      {agent.name}
                    </span>
                    <span className="tabular text-[11px] text-[var(--color-faint)]">
                      {agent.connector_ids.length} connectors
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <Empty title="No agents registered" />
            )}
          </Panel>
        </div>
      </div>

      <p className="px-1 pb-1 text-[11px] leading-relaxed text-[var(--color-faint)]">
        {BLOCKED_ITEMS.length} further sections are defined but not yet backed
        by an API. They appear in the sidebar, disabled, with the milestone
        that delivers each. Nothing in this console is mock data.
      </p>
    </div>
  );
}
