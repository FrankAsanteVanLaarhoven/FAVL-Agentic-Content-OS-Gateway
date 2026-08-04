"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Empty,
  Field,
  Panel,
  Pill,
  StatusDot,
  relativeTime,
} from "@/components/primitives";
import { connectorTone, type Connector } from "@/lib/types";

async function get<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

/** Config keys whose value must never be rendered, defence in depth. */
const SENSITIVE = /(secret|token|password|key|credential)/i;

export function ConnectorsView() {
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const { data, isLoading } = useQuery<Connector[]>({
    queryKey: ["connectors", includeDeleted],
    queryFn: () =>
      get(`/api/connectors${includeDeleted ? "?include_deleted=true" : ""}`),
    refetchInterval: 10000,
  });

  const selected = data?.find((c) => c.id === selectedId) ?? null;

  return (
    <div className="grid h-full min-h-0 grid-cols-1 gap-3 p-3 xl:grid-cols-[minmax(0,1fr)_380px]">
      <Panel
        title={`Connectors · ${data?.length ?? 0}`}
        action={
          <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-[var(--color-faint)]">
            <input
              type="checkbox"
              checked={includeDeleted}
              onChange={(e) => setIncludeDeleted(e.target.checked)}
              className="size-3 accent-[var(--color-info)]"
            />
            include deleted
          </label>
        }
      >
        {isLoading ? (
          <Empty title="Loading…" />
        ) : data?.length ? (
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-[var(--color-surface)]">
              <tr className="text-[10px] uppercase tracking-[0.08em] text-[var(--color-faint)]">
                <th className="hairline px-3 py-1.5 text-left font-medium">Name</th>
                <th className="hairline w-24 px-3 py-1.5 text-left font-medium">Kind</th>
                <th className="hairline w-44 px-3 py-1.5 text-left font-medium">Status</th>
                <th className="hairline w-16 px-3 py-1.5 text-right font-medium">Ver</th>
                <th className="hairline w-24 px-3 py-1.5 text-right font-medium">Created</th>
              </tr>
            </thead>
            <tbody>
              {data.map((connector) => (
                <tr
                  key={connector.id}
                  onClick={() => setSelectedId(connector.id)}
                  className={`cursor-pointer border-b border-[var(--color-line)] transition-colors last:border-0 ${
                    selectedId === connector.id
                      ? "bg-[var(--color-raised)]"
                      : "hover:bg-[var(--color-raised)]/60"
                  }`}
                >
                  <td className="max-w-0 truncate px-3 py-1.5 text-[var(--color-ink)]">
                    {connector.name}
                  </td>
                  <td className="px-3 py-1.5">
                    <Pill>{connector.kind}</Pill>
                  </td>
                  <td className="px-3 py-1.5">
                    <StatusDot
                      tone={connectorTone(connector.status)}
                      label={connector.status}
                    />
                  </td>
                  <td className="tabular px-3 py-1.5 text-right text-[var(--color-muted)]">
                    {connector.version}
                  </td>
                  <td className="px-3 py-1.5 text-right text-[var(--color-faint)]">
                    {relativeTime(connector.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty title="No connectors registered" />
        )}
      </Panel>

      <Panel title="Inspector" className="hidden xl:flex">
        {selected ? (
          <div className="animate-state-in">
            <div className="hairline px-3 py-2.5">
              <p className="truncate text-sm">{selected.name}</p>
              <p className="truncate font-mono text-[11px] text-[var(--color-faint)]">
                {selected.id}
              </p>
            </div>
            <dl className="hairline py-1">
              <Field label="Kind">{selected.kind}</Field>
              <Field label="Status">
                <StatusDot
                  tone={connectorTone(selected.status)}
                  label={selected.status}
                />
              </Field>
              <Field label="Version" mono>
                {selected.version}
              </Field>
              <Field label="Created">{relativeTime(selected.created_at)}</Field>
              {selected.deletion_requested_at ? (
                <Field label="Deletion req.">
                  <span className="text-[var(--color-warn)]">
                    {relativeTime(selected.deletion_requested_at)}
                  </span>
                </Field>
              ) : null}
              <Field label="Scopes" mono>
                {selected.scopes.length ? selected.scopes.join(", ") : "—"}
              </Field>
            </dl>
            <div className="py-1">
              <h3 className="px-3 pb-1 pt-1.5 text-[10px] uppercase tracking-[0.1em] text-[var(--color-faint)]">
                Configuration
              </h3>
              <dl>
                {Object.entries(selected.config ?? {}).map(([key, value]) => (
                  <Field key={key} label={key} mono>
                    {SENSITIVE.test(key) && !String(value).startsWith("env:") ? (
                      // The API already rejects literal secrets; this is the
                      // second line of defence at the render boundary.
                      <span className="text-[var(--color-err)]">redacted</span>
                    ) : (
                      <span className="text-[var(--color-muted)]">
                        {typeof value === "object"
                          ? JSON.stringify(value)
                          : String(value)}
                      </span>
                    )}
                  </Field>
                ))}
              </dl>
            </div>
          </div>
        ) : (
          <Empty
            title="Nothing selected"
            body="Select a connector to inspect its lifecycle state, version and configuration. Secret references are shown; secret values are never returned by the API."
          />
        )}
      </Panel>
    </div>
  );
}
