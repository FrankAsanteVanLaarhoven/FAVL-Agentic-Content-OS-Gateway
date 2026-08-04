"use client";

import { useEffect, useState, type ReactNode } from "react";
import type { Tone } from "@/lib/types";

const TONE_TEXT: Record<Tone, string> = {
  ok: "text-[var(--color-ok)]",
  warn: "text-[var(--color-warn)]",
  err: "text-[var(--color-err)]",
  info: "text-[var(--color-info)]",
  idle: "text-[var(--color-faint)]",
};

const TONE_BG: Record<Tone, string> = {
  ok: "bg-[var(--color-ok)]",
  warn: "bg-[var(--color-warn)]",
  err: "bg-[var(--color-err)]",
  info: "bg-[var(--color-info)]",
  idle: "bg-[var(--color-faint)]",
};

/**
 * A 6px dot plus a text label. Never colour alone: a status that is only
 * distinguishable by hue is unreadable to a colour-blind operator, and this
 * console uses colour exclusively for state.
 */
export function StatusDot({
  tone,
  label,
  className = "",
}: {
  tone: Tone;
  label?: string;
  className?: string;
}) {
  return (
    <span className={`inline-flex items-center gap-2 ${className}`}>
      <span
        aria-hidden
        className={`size-1.5 shrink-0 rounded-full ${TONE_BG[tone]}`}
      />
      {label ? (
        <span className={`text-xs ${TONE_TEXT[tone]}`}>{label}</span>
      ) : null}
    </span>
  );
}

export function Pill({
  tone = "idle",
  children,
}: {
  tone?: Tone;
  children: ReactNode;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border border-[var(--color-line)] bg-[var(--color-raised)] px-1.5 py-0.5 font-mono text-[11px] leading-4 ${TONE_TEXT[tone]}`}
    >
      {children}
    </span>
  );
}

export function Panel({
  title,
  action,
  children,
  className = "",
}: {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`flex min-h-0 flex-col rounded-[var(--radius-lg)] border border-[var(--color-line)] bg-[var(--color-surface)] ${className}`}
    >
      {title ? (
        <header className="hairline flex h-10 shrink-0 items-center justify-between px-3">
          <h2 className="text-[11px] font-medium uppercase tracking-[0.08em] text-[var(--color-muted)]">
            {title}
          </h2>
          {action}
        </header>
      ) : null}
      <div className="min-h-0 flex-1 overflow-auto">{children}</div>
    </section>
  );
}

/** A single number with its label. Deliberately not a card with a chart. */
export function Stat({
  label,
  value,
  tone = "idle",
  hint,
}: {
  label: string;
  value: string | number | null;
  tone?: Tone;
  hint?: string;
}) {
  return (
    <div className="flex flex-col gap-1 px-3 py-2.5">
      <span className="text-[11px] uppercase tracking-[0.08em] text-[var(--color-muted)]">
        {label}
      </span>
      <span
        className={`tabular text-xl font-medium leading-none ${value === null ? "text-[var(--color-faint)]" : TONE_TEXT[tone]}`}
        title={hint}
      >
        {value === null ? "—" : value}
      </span>
    </div>
  );
}

export function Empty({
  title,
  body,
}: {
  title: string;
  body?: ReactNode;
}) {
  return (
    <div className="flex h-full min-h-40 flex-col items-center justify-center gap-2 px-8 py-12 text-center">
      <p className="text-sm text-[var(--color-muted)]">{title}</p>
      {body ? (
        <p className="max-w-md text-xs leading-relaxed text-[var(--color-faint)]">
          {body}
        </p>
      ) : null}
    </div>
  );
}

export function Field({
  label,
  children,
  mono = false,
}: {
  label: string;
  children: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="grid grid-cols-[7.5rem_1fr] items-baseline gap-3 px-3 py-1.5">
      <dt className="text-[11px] uppercase tracking-[0.06em] text-[var(--color-muted)]">
        {label}
      </dt>
      <dd
        className={`min-w-0 break-words text-xs text-[var(--color-ink)] ${mono ? "font-mono" : ""}`}
      >
        {children}
      </dd>
    </div>
  );
}

export function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const delta = Date.now() - new Date(iso).getTime();
  const seconds = Math.round(delta / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function clockTime(iso: string | null): string {
  if (!iso) return "--:--:--";
  return new Date(iso).toLocaleTimeString("en-GB", { hour12: false });
}

/**
 * True only after the first client render.
 *
 * Times formatted with toLocaleTimeString depend on the renderer's timezone,
 * so emitting one during SSR and a different one on hydration is a guaranteed
 * mismatch. Anything timezone-dependent waits for this.
 */
export function useMounted(): boolean {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  return mounted;
}

/** Renders a timezone-dependent clock only once mounted. */
export function ClientClock({ iso }: { iso: string | null }): ReactNode {
  const mounted = useMounted();
  return mounted ? clockTime(iso) : "--:--:--";
}
