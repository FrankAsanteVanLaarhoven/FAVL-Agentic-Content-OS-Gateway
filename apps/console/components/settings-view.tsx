"use client";

import { useQuery } from "@tanstack/react-query";
import { Field, Panel, StatusDot } from "@/components/primitives";
import { BLOCKED_ITEMS } from "@/lib/nav";
import type { Readiness } from "@/lib/types";

type HealthPayload = { orchestrator: Readiness | null; registry: Readiness | null };

export function SettingsView() {
  const { data } = useQuery<HealthPayload>({
    queryKey: ["health"],
    queryFn: async () => (await fetch("/api/health")).json(),
    refetchInterval: 10000,
  });

  return (
    <div className="grid h-full grid-cols-1 gap-3 overflow-auto p-3 lg:grid-cols-2">
      <Panel title="Environment">
        <dl className="py-1">
          <Field label="Deployment">local development (docker compose)</Field>
          <Field label="Identity">Keycloak development mode, repository realm</Field>
          <Field label="Secrets">environment-backed resolver (M1.7 replaces)</Field>
          <Field label="Gateway">Apache APISIX 3.17, OIDC bearer-only</Field>
          <Field label="Adapters" mono>
            {data?.registry?.adapters?.join(", ") ?? "—"}
          </Field>
        </dl>
        <div className="border-t border-[var(--color-line)] px-3 py-2.5">
          <p className="text-[11px] leading-relaxed text-[var(--color-faint)]">
            This console reaches the platform through APISIX using a server-side
            token. No credential is ever sent to the browser. Production identity
            hardening is not evaluated in this environment.
          </p>
        </div>
      </Panel>

      <Panel title="Service readiness">
        <dl className="py-1">
          <Field label="Orchestrator">
            <StatusDot
              tone={data?.orchestrator?.status === "ready" ? "ok" : "err"}
              label={data?.orchestrator?.status ?? "unreachable"}
            />
          </Field>
          <Field label="Registry">
            <StatusDot
              tone={data?.registry?.status === "ready" ? "ok" : "err"}
              label={data?.registry?.status ?? "unreachable"}
            />
          </Field>
          <Field label="Migrations">
            <StatusDot
              tone={data?.registry?.migrations_current ? "ok" : "warn"}
              label={data?.registry?.migrations_current ? "current" : "behind"}
            />
          </Field>
          <Field label="JetStream">
            <StatusDot
              tone={data?.registry?.nats_connected ? "ok" : "err"}
              label={data?.registry?.nats_connected ? "connected" : "unavailable"}
            />
          </Field>
        </dl>
      </Panel>

      <Panel title={`Roadmap · ${BLOCKED_ITEMS.length} sections pending`} className="lg:col-span-2">
        <ul>
          {BLOCKED_ITEMS.map((item) => (
            <li key={item.slug} className="border-b border-[var(--color-line)] px-3 py-2 last:border-0">
              <div className="flex items-baseline gap-2">
                <item.icon className="size-3.5 shrink-0 translate-y-0.5 text-[var(--color-faint)]" strokeWidth={1.75} />
                <span className="text-xs text-[var(--color-ink)]">{item.label}</span>
                <span className="rounded-[3px] border border-[var(--color-line)] px-1.5 font-mono text-[10px] text-[var(--color-faint)]">
                  {item.blockedBy}
                </span>
              </div>
              <p className="mt-1 pl-5.5 text-[11px] leading-relaxed text-[var(--color-faint)]">
                {item.reason}
              </p>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}
