"use client";

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { AnimatePresence, motion } from "motion/react";
import { X } from "lucide-react";
import type { NavItem } from "@/lib/nav";

/**
 * Explains why a section does not exist yet.
 *
 * This is the honest alternative to hiding the nav item or filling it with
 * mock data. It names the blocking milestone and the concrete reason, so the
 * console never implies capability it does not have.
 */

const Ctx = createContext<(item: NavItem) => void>(() => {});

export function useUnavailable() {
  return useContext(Ctx);
}

export function UnavailableProvider({ children }: { children: ReactNode }) {
  const [item, setItem] = useState<NavItem | null>(null);
  const show = useCallback((next: NavItem) => setItem(next), []);
  const value = useMemo(() => show, [show]);

  return (
    <Ctx.Provider value={value}>
      {children}
      <AnimatePresence>
        {item && (
          <motion.aside
            key="unavailable"
            initial={{ opacity: 0, x: 8 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 8 }}
            transition={{ duration: 0.18, ease: [0.25, 1, 0.5, 1] }}
            className="pointer-events-auto fixed bottom-9 right-3 z-40 w-[340px] rounded-[var(--radius-lg)] border border-[var(--color-line)] bg-[var(--color-surface)] shadow-2xl shadow-black/50"
          >
            <header className="hairline flex h-9 items-center justify-between px-3">
              <span className="flex items-center gap-2 text-[11px] uppercase tracking-[0.08em] text-[var(--color-muted)]">
                <item.icon className="size-3.5" strokeWidth={1.75} />
                {item.label}
              </span>
              <button
                type="button"
                onClick={() => setItem(null)}
                aria-label="Dismiss"
                className="text-[var(--color-faint)] transition-colors hover:text-[var(--color-ink)]"
              >
                <X className="size-3.5" />
              </button>
            </header>
            <div className="space-y-2.5 p-3">
              <p className="text-xs leading-relaxed text-[var(--color-muted)]">
                {item.reason}
              </p>
              <p className="flex items-center gap-2 text-[11px] text-[var(--color-faint)]">
                <span className="rounded-[3px] border border-[var(--color-line)] px-1.5 py-0.5 font-mono">
                  {item.blockedBy}
                </span>
                delivers this section
              </p>
            </div>
          </motion.aside>
        )}
      </AnimatePresence>
    </Ctx.Provider>
  );
}
