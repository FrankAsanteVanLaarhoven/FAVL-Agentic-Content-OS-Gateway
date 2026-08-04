"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { motion } from "motion/react";
import {
  Empty,
  Field,
  Panel,
  Pill,
  StatusDot,
  ClientClock,
  relativeTime,
} from "@/components/primitives";
import {
  invocationTone,
  type Connector,
  type Invocation,
  type InvocationStatus,
} from "@/lib/types";

const FILTERS: { label: string; value: InvocationStatus | "all" }[] = [
  { label: "All", value: "all" },
  { label: "Succeeded", value: "succeeded" },
  { label: "Failed", value: "failed_terminal" },
  { label: "Retryable", value: "failed_retryable" },
  { label: "Timed out", value: "timed_out" },
  { label: "Running", value: "running" },
];

async function get<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

export function EventConsole() {
  const [filter, setFilter] = useState<InvocationStatus | "all">("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const { data, isLoading, dataUpdatedAt } = useQuery<Invocation[]>({
    queryKey: ["invocations", filter],
    queryFn: () =>
      get(
        `/api/invocations?limit=100${filter === "all" ? "" : `&status=${filter}`}`,
      ),
    // Polled, not streamed. A NATS-to-SSE bridge is the next step; until then
    // the interval is stated in the UI rather than implied to be live.
    refetchInterval: 3000,
  });

  const connectors = useQuery<Connector[]>({
    queryKey: ["connectors", "all"],
    queryFn: () => get("/api/connectors?include_deleted=true"),
  });

  const nameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const c of connectors.data ?? []) map.set(c.id, c.name);
    return map;
  }, [connectors.data]);

  const selected = data?.find((row) => row.id === selectedId) ?? null;

  return (
    <div className="grid h-full min-h-0 grid-cols-1 gap-3 p-3 xl:grid-cols-[minmax(0,1fr)_380px]">
      <Panel
        title={
          <span className="flex items-center gap-3">
            Event console
            <span className="font-normal normal-case tracking-normal text-[var(--color-faint)]">
              polling every 3s · updated{" "}
              <ClientClock iso={new Date(dataUpdatedAt).toISOString()} />
            </span>
          </span>
        }
        action={
          <div className="flex items-center gap-0.5">
            {FILTERS.map((option) => (
              <button
                key={option.value}
                type="button"
                onClick={() => setFilter(option.value)}
                className={`rounded-[var(--radius-sm)] px-2 py-0.5 text-[11px] transition-colors ${
                  filter === option.value
                    ? "bg-[var(--color-raised)] text-[var(--color-ink)]"
                    : "text-[var(--color-faint)] hover:text-[var(--color-muted)]"
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
        }
      >
        {isLoading ? (
          <Empty title="Loading…" />
        ) : data?.length ? (
          <table className="w-full border-collapse text-xs">
            <thead className="sticky top-0 z-10 bg-[var(--color-surface)]">
              <tr className="text-[10px] uppercase tracking-[0.08em] text-[var(--color-faint)]">
                <th className="hairline w-20 px-3 py-1.5 text-left font-medium">
                  Time
                </th>
                <th className="hairline w-32 px-3 py-1.5 text-left font-medium">
                  Status
                </th>
                <th className="hairline w-20 px-3 py-1.5 text-left font-medium">
                  Kind
                </th>
                <th className="hairline px-3 py-1.5 text-left font-medium">
                  Connector
                </th>
                <th className="hairline px-3 py-1.5 text-left font-medium">
                  Operation
                </th>
                <th className="hairline w-20 px-3 py-1.5 text-right font-medium">
                  Duration
                </th>
                <th className="hairline w-44 px-3 py-1.5 text-left font-medium">
                  Error
                </th>
              </tr>
            </thead>
            <tbody>
              {data.map((row, index) => (
                <motion.tr
                  key={row.id}
                  // Only the newest rows animate in, once. A table where every
                  // row animates on each poll is unreadable.
                  initial={index < 3 ? { opacity: 0, y: -2 } : false}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.15, ease: [0.25, 1, 0.5, 1] }}
                  role="button"
                  tabIndex={0}
                  aria-pressed={selectedId === row.id}
                  onClick={() => setSelectedId(row.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setSelectedId(row.id);
                    }
                  }}
                  className={`cursor-pointer border-b border-[var(--color-line)] transition-colors last:border-0 focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--color-info)] ${
                    selectedId === row.id
                      ? "bg-[var(--color-raised)]"
                      : "hover:bg-[var(--color-raised)]/60"
                  }`}
                >
                  <td className="px-3 py-1.5 font-mono text-[11px] text-[var(--color-faint)]">
                    <ClientClock iso={row.completed_at ?? row.started_at} />
                  </td>
                  <td className="px-3 py-1.5">
                    <StatusDot
                      tone={invocationTone(row.status)}
                      label={row.status}
                    />
                  </td>
                  <td className="px-3 py-1.5 text-[var(--color-muted)]">
                    {row.adapter_kind}
                  </td>
                  <td className="max-w-0 truncate px-3 py-1.5 text-[var(--color-ink)]">
                    {nameById.get(row.connector_id) ?? row.connector_id.slice(0, 8)}
                  </td>
                  <td className="max-w-0 truncate px-3 py-1.5 text-[var(--color-muted)]">
                    {row.operation || "—"}
                  </td>
                  <td className="tabular px-3 py-1.5 text-right text-[var(--color-muted)]">
                    {row.duration_ms === null
                      ? "—"
                      : `${Math.round(row.duration_ms)}ms`}
                  </td>
                  <td className="max-w-0 truncate px-3 py-1.5">
                    {row.error_code ? (
                      <span className="font-mono text-[10px] text-[var(--color-err)]">
                        {row.error_code}
                      </span>
                    ) : (
                      <span className="text-[var(--color-faint)]">—</span>
                    )}
                  </td>
                </motion.tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty
            title="No invocations match this filter"
            body="Invocations appear here as connectors are called."
          />
        )}
      </Panel>

      <Inspector invocation={selected} connectorName={
        selected ? nameById.get(selected.connector_id) ?? null : null
      } />
    </div>
  );
}

/** Contextual detail for whatever is selected. Never a modal. */
function Inspector({
  invocation,
  connectorName,
}: {
  invocation: Invocation | null;
  connectorName: string | null;
}) {
  if (!invocation) {
    return (
      <Panel title="Inspector" className="hidden xl:flex">
        <Empty
          title="Nothing selected"
          body="Select an invocation to see its lifecycle, timing, error classification and audit identifiers."
        />
      </Panel>
    );
  }

  const timeline = [
    { label: "Accepted", at: null as string | null, always: true },
    { label: "Started", at: invocation.started_at, always: false },
    {
      label:
        invocation.status === "succeeded"
          ? "Succeeded"
          : invocation.status === "timed_out"
            ? "Timed out"
            : "Failed",
      at: invocation.completed_at,
      always: false,
    },
  ];

  return (
    <Panel
      title="Inspector"
      className="hidden xl:flex"
      action={
        <StatusDot
          tone={invocationTone(invocation.status)}
          label={invocation.status}
        />
      }
    >
      <div className="animate-state-in">
        <div className="hairline px-3 py-2.5">
          <p className="truncate text-sm text-[var(--color-ink)]">
            {invocation.operation || "(no operation)"}
          </p>
          <p className="truncate font-mono text-[11px] text-[var(--color-faint)]">
            {invocation.id}
          </p>
        </div>

        <div className="hairline py-1">
          <h3 className="px-3 pb-1 pt-1.5 text-[10px] uppercase tracking-[0.1em] text-[var(--color-faint)]">
            Timeline
          </h3>
          <ol className="px-3 pb-2">
            {timeline.map((step, index) => (
              <li
                key={step.label}
                className="relative flex items-baseline gap-3 py-1 pl-4"
              >
                <span
                  aria-hidden
                  className="absolute left-0 top-2.5 size-1.5 rounded-full bg-[var(--color-line-strong)]"
                />
                {index < timeline.length - 1 && (
                  <span
                    aria-hidden
                    className="absolute left-[2.5px] top-4 h-full w-px bg-[var(--color-line)]"
                  />
                )}
                <span className="flex-1 text-xs text-[var(--color-muted)]">
                  {step.label}
                </span>
                <span className="tabular font-mono text-[11px] text-[var(--color-faint)]">
                  {step.at ? <ClientClock iso={step.at} /> : "—"}
                </span>
              </li>
            ))}
          </ol>
        </div>

        <dl className="hairline py-1">
          <Field label="Adapter">{invocation.adapter_kind}</Field>
          <Field label="Connector">{connectorName ?? "—"}</Field>
          <Field label="Version" mono>
            {invocation.connector_version}
          </Field>
          <Field label="Duration">
            {invocation.duration_ms === null
              ? "—"
              : `${invocation.duration_ms.toFixed(1)} ms`}
          </Field>
          <Field label="Attempt" mono>
            {invocation.attempt}
          </Field>
          <Field label="Deadline">{relativeTime(invocation.deadline_at)}</Field>
        </dl>

        {invocation.error_code ? (
          <dl className="hairline py-1">
            <Field label="Error code" mono>
              <span className="text-[var(--color-err)]">
                {invocation.error_code}
              </span>
            </Field>
            <Field label="Retryable">
              <Pill tone={invocation.retryable ? "warn" : "err"}>
                {invocation.retryable ? "yes" : "no"}
              </Pill>
            </Field>
            {invocation.error_detail ? (
              <Field label="Detail" mono>
                <span className="text-[var(--color-muted)]">
                  {invocation.error_detail}
                </span>
              </Field>
            ) : null}
          </dl>
        ) : null}

        <dl className="hairline py-1">
          <h3 className="px-3 pb-1 pt-1.5 text-[10px] uppercase tracking-[0.1em] text-[var(--color-faint)]">
            Audit
          </h3>
          <Field label="Actor" mono>
            {invocation.actor_id}
          </Field>
          <Field label="Tenant" mono>
            {invocation.tenant_id}
          </Field>
          <Field label="Idempotency" mono>
            <span className="text-[var(--color-muted)]">
              {invocation.idempotency_key}
            </span>
          </Field>
          <Field label="Provider ref" mono>
            {invocation.provider_request_id ?? "—"}
          </Field>
          <Field label="Trace" mono>
            {invocation.trace_id ?? "—"}
          </Field>
        </dl>

        {invocation.output ? (
          <div className="py-1">
            <h3 className="px-3 pb-1 pt-1.5 text-[10px] uppercase tracking-[0.1em] text-[var(--color-faint)]">
              Output
            </h3>
            <pre className="mx-3 mb-3 overflow-x-auto rounded-[var(--radius-sm)] border border-[var(--color-line)] bg-[var(--color-void)] p-2.5 font-mono text-[11px] leading-relaxed text-[var(--color-muted)]">
              {JSON.stringify(invocation.output, null, 2)}
            </pre>
          </div>
        ) : null}
      </div>
    </Panel>
  );
}
