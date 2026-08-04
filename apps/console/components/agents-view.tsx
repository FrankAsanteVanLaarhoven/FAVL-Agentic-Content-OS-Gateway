"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Empty, Field, Panel, Pill, relativeTime } from "@/components/primitives";
import type { Agent, Connector } from "@/lib/types";

async function get<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

export function AgentsView() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const { data, isLoading } = useQuery<Agent[]>({
    queryKey: ["agents"],
    queryFn: () => get("/api/agents"),
    refetchInterval: 10000,
  });
  const connectors = useQuery<Connector[]>({
    queryKey: ["connectors", "all"],
    queryFn: () => get("/api/connectors?include_deleted=true"),
  });

  const selected = data?.find((a) => a.id === selectedId) ?? null;
  const nameFor = (id: string) =>
    connectors.data?.find((c) => c.id === id)?.name ?? id.slice(0, 8);

  return (
    <div className="grid h-full min-h-0 grid-cols-1 gap-3 p-3 xl:grid-cols-[minmax(0,1fr)_380px]">
      <Panel title={`Agents · ${data?.length ?? 0}`}>
        {isLoading ? (
          <Empty title="Loading…" />
        ) : data?.length ? (
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-[var(--color-surface)]">
              <tr className="text-[10px] uppercase tracking-[0.08em] text-[var(--color-faint)]">
                <th className="hairline px-3 py-1.5 text-left font-medium">Name</th>
                <th className="hairline px-3 py-1.5 text-left font-medium">Description</th>
                <th className="hairline w-24 px-3 py-1.5 text-right font-medium">Connectors</th>
                <th className="hairline w-24 px-3 py-1.5 text-right font-medium">Created</th>
              </tr>
            </thead>
            <tbody>
              {data.map((agent) => (
                <tr
                  key={agent.id}
                  onClick={() => setSelectedId(agent.id)}
                  className={`cursor-pointer border-b border-[var(--color-line)] transition-colors last:border-0 ${
                    selectedId === agent.id
                      ? "bg-[var(--color-raised)]"
                      : "hover:bg-[var(--color-raised)]/60"
                  }`}
                >
                  <td className="max-w-0 truncate px-3 py-1.5 text-[var(--color-ink)]">{agent.name}</td>
                  <td className="max-w-0 truncate px-3 py-1.5 text-[var(--color-muted)]">
                    {agent.description || "—"}
                  </td>
                  <td className="tabular px-3 py-1.5 text-right text-[var(--color-muted)]">
                    {agent.connector_ids.length}
                  </td>
                  <td className="px-3 py-1.5 text-right text-[var(--color-faint)]">
                    {relativeTime(agent.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty title="No agents registered" />
        )}
      </Panel>

      <Panel title="Inspector" className="hidden xl:flex">
        {selected ? (
          <div className="animate-state-in">
            <div className="hairline px-3 py-2.5">
              <p className="truncate text-sm">{selected.name}</p>
              <p className="truncate font-mono text-[11px] text-[var(--color-faint)]">{selected.id}</p>
            </div>
            <dl className="hairline py-1">
              <Field label="Description">{selected.description || "—"}</Field>
              <Field label="Created">{relativeTime(selected.created_at)}</Field>
            </dl>
            <div className="py-1">
              <h3 className="px-3 pb-1 pt-1.5 text-[10px] uppercase tracking-[0.1em] text-[var(--color-faint)]">
                Connectors
              </h3>
              {selected.connector_ids.length ? (
                <ul className="px-3 pb-2">
                  {selected.connector_ids.map((id) => (
                    <li key={id} className="flex items-center gap-2 py-1">
                      <Pill>{nameFor(id)}</Pill>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="px-3 pb-2 text-xs text-[var(--color-faint)]">
                  No connectors attached.
                </p>
              )}
            </div>
            <div className="border-t border-[var(--color-line)] px-3 py-2.5">
              <p className="text-[11px] leading-relaxed text-[var(--color-faint)]">
                Agents currently fan out to their connectors sequentially. Planning,
                memory and routing arrive with the workflow engine in M2.
              </p>
            </div>
          </div>
        ) : (
          <Empty title="Nothing selected" body="Select an agent to inspect its connectors." />
        )}
      </Panel>
    </div>
  );
}
