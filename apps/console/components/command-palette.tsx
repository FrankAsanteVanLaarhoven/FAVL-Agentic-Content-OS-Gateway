"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Command } from "cmdk";
import { AnimatePresence, motion } from "motion/react";
import { CornerDownLeft, Lock, Search } from "lucide-react";
import { NAV } from "@/lib/nav";
import { useUnavailable } from "@/components/unavailable";

/**
 * ⌘K is the primary way to move around; the sidebar is the fallback.
 *
 * Unavailable sections appear here too rather than being filtered out —
 * searching for "Workflows" and finding nothing suggests the concept does
 * not exist, when the truth is it is not built yet.
 */
export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const router = useRouter();
  const showUnavailable = useUnavailable();
  // Remember what had focus so it can be restored on close; otherwise focus
  // falls back to <body> and keyboard users lose their place entirely.
  const opener = useRef<HTMLElement | null>(null);

  const close = useCallback(() => {
    setOpen(false);
    opener.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "k" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        opener.current = document.activeElement as HTMLElement | null;
        setOpen((v) => !v);
      }
      // The UI advertises ESC, so it has to work.
      if (event.key === "Escape") {
        setOpen((wasOpen) => {
          if (wasOpen) opener.current?.focus();
          return false;
        });
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  return (
    <>
      <button
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={(event) => {
          opener.current = event.currentTarget;
          setOpen(true);
        }}
        className="group flex h-7 w-[280px] items-center gap-2 rounded-[var(--radius-md)] border border-[var(--color-line)] bg-[var(--color-void)] px-2.5 text-left transition-colors hover:border-[var(--color-line-strong)]"
      >
        <Search
          className="size-3.5 shrink-0 text-[var(--color-faint)]"
          strokeWidth={1.75}
        />
        <span className="flex-1 truncate text-xs text-[var(--color-faint)]">
          Search or run a command
        </span>
        <kbd className="rounded-[3px] border border-[var(--color-line)] px-1 font-mono text-[10px] text-[var(--color-faint)]">
          ⌘K
        </kbd>
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            key="scrim"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.12 }}
            className="fixed inset-0 z-50 bg-black/60 backdrop-blur-[2px]"
            onClick={close}
          >
            <motion.div
              initial={{ opacity: 0, y: -6, scale: 0.985 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -6, scale: 0.985 }}
              transition={{ duration: 0.16, ease: [0.25, 1, 0.5, 1] }}
              role="dialog"
              aria-modal="true"
              aria-label="Command palette"
              onClick={(e) => e.stopPropagation()}
              className="mx-auto mt-[12vh] w-[560px] max-w-[92vw] overflow-hidden rounded-[var(--radius-lg)] border border-[var(--color-line-strong)] bg-[var(--color-surface)] shadow-2xl shadow-black/60"
            >
              <Command
                loop
                onKeyDown={(event) => {
                  if (event.key === "Escape") {
                    event.preventDefault();
                    close();
                  }
                }}
                className="[&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:pt-2.5 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-[0.1em] [&_[cmdk-group-heading]]:text-[var(--color-faint)]"
              >
                <div className="hairline flex h-11 items-center gap-2.5 px-3.5">
                  <Search
                    className="size-4 shrink-0 text-[var(--color-faint)]"
                    strokeWidth={1.75}
                  />
                  <Command.Input
                    autoFocus
                    placeholder="Search sections, agents, connectors…"
                    className="h-full flex-1 bg-transparent text-sm text-[var(--color-ink)] outline-none placeholder:text-[var(--color-faint)]"
                  />
                  <kbd className="rounded-[3px] border border-[var(--color-line)] px-1 font-mono text-[10px] text-[var(--color-faint)]">
                    ESC
                  </kbd>
                </div>

                <Command.List className="max-h-[340px] overflow-y-auto p-1.5">
                  <Command.Empty className="px-3 py-8 text-center text-xs text-[var(--color-faint)]">
                    Nothing matches.
                  </Command.Empty>

                  {NAV.map((group) => (
                    <Command.Group key={group.heading} heading={group.heading}>
                      {group.items.map((item) => {
                        const Icon = item.icon;
                        const blocked = !item.href;
                        return (
                          <Command.Item
                            key={item.slug}
                            value={`${item.label} ${group.heading} ${item.blockedBy ?? ""}`}
                            onSelect={() => {
                              close();
                              if (blocked) showUnavailable(item);
                              else router.push(item.href!);
                            }}
                            className={`group flex h-8 cursor-pointer items-center gap-2.5 rounded-[var(--radius-sm)] px-2.5 text-[13px] data-[selected=true]:bg-[var(--color-raised)] ${
                              blocked
                                ? "text-[var(--color-faint)]"
                                : "text-[var(--color-muted)] data-[selected=true]:text-[var(--color-ink)]"
                            }`}
                          >
                            <Icon className="size-4 shrink-0" strokeWidth={1.75} />
                            <span className="flex-1 truncate">{item.label}</span>
                            {blocked ? (
                              <span className="flex items-center gap-1 font-mono text-[10px] uppercase text-[var(--color-faint)]">
                                <Lock className="size-3" />
                                {item.blockedBy}
                              </span>
                            ) : (
                              <CornerDownLeft className="size-3 opacity-0 group-data-[selected=true]:opacity-40" />
                            )}
                          </Command.Item>
                        );
                      })}
                    </Command.Group>
                  ))}
                </Command.List>
              </Command>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}
