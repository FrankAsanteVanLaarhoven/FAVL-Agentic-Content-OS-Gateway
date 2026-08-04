"use client";

import { useQuery } from "@tanstack/react-query";
import {
  ClientClock,
  Empty,
  Panel,
  Pill,
  StatusDot,
} from "@/components/primitives";
import { invocationTone, type Invocation } from "@/lib/types";

async function get<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

/**
 * The audit trail is the invocation record, not a separate log.
 *
 * Every invocation persists actor, tenant, idempotency key, connector version
 * and provider reference, so the operational history and the audit history
 * are the same rows rather than two stores that can disagree.
 */
export function AuditTrail() {
  const { data, isLoading } = useQuery<Invocation[]>({
    queryKey: ["invocations", "audit"],
    queryFn: () => get("/api/invocations?limit=200"),
    refetchInterval: 10000,
  });

  return (
    <div className="h-full p-3">
      <Panel
        title={`Audit trail · ${data?.length ?? 0} invocations`}
        action={
          <span className="text-[11px] text-[var(--color-faint)]">
            actor · tenant · connector version · provider reference
          </span>
        }
        className="h-full"
      >
        {isLoading ? (
          <Empty title="Loading…" />
        ) : data?.length ? (
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-[var(--color-surface)]">
              <tr className="text-[10px] uppercase tracking-[0.08em] text-[var(--color-faint)]">
                <th className="hairline w-20 px-3 py-1.5 text-left font-medium">Time</th>
                <th className="hairline w-32 px-3 py-1.5 text-left font-medium">Actor</th>
                <th className="hairline w-20 px-3 py-1.5 text-left font-medium">Tenant</th>
                <th className="hairline w-36 px-3 py-1.5 text-left font-medium">Status</th>
                <th className="hairline px-3 py-1.5 text-left font-medium">Operation</th>
                <th className="hairline w-14 px-3 py-1.5 text-right font-medium">C.ver</th>
                <th className="hairline px-3 py-1.5 text-left font-medium">Idempotency key</th>
                <th className="hairline px-3 py-1.5 text-left font-medium">Provider ref</th>
              </tr>
            </thead>
            <tbody>
              {data.map((row) => (
                <tr key={row.id} className="border-b border-[var(--color-line)] last:border-0">
                  <td className="px-3 py-1.5 font-mono text-[11px] text-[var(--color-faint)]">
                    <ClientClock iso={row.completed_at ?? row.started_at} />
                  </td>
                  <td className="max-w-0 truncate px-3 py-1.5 font-mono text-[11px] text-[var(--color-ink)]">
                    {row.actor_id}
                  </td>
                  <td className="px-3 py-1.5"><Pill>{row.tenant_id}</Pill></td>
                  <td className="px-3 py-1.5">
                    <StatusDot tone={invocationTone(row.status)} label={row.status} />
                  </td>
                  <td className="max-w-0 truncate px-3 py-1.5 text-[var(--color-muted)]">
                    {row.operation || "—"}
                  </td>
                  <td className="tabular px-3 py-1.5 text-right text-[var(--color-muted)]">
                    {row.connector_version}
                  </td>
                  <td className="max-w-0 truncate px-3 py-1.5 font-mono text-[10px] text-[var(--color-faint)]">
                    {row.idempotency_key}
                  </td>
                  <td className="max-w-0 truncate px-3 py-1.5 font-mono text-[10px] text-[var(--color-faint)]">
                    {row.provider_request_id ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty
            title="No audited actions yet"
            body="Connector invocations are the audit record. Connector lifecycle auditing arrives with M1.4."
          />
        )}
      </Panel>
    </div>
  );
}
