"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { NAV, type NavItem } from "@/lib/nav";
import { useUnavailable } from "@/components/unavailable";

export function Sidebar() {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);
  const show = useUnavailable();

  return (
    <nav
      aria-label="Platform sections"
      data-collapsed={collapsed}
      className={`flex shrink-0 flex-col border-r border-[var(--color-line)] bg-[var(--color-surface)] transition-[width] duration-200 ease-[var(--ease-out-quart)] ${
        collapsed ? "w-[52px]" : "w-[212px]"
      }`}
    >
      <div className="flex h-11 shrink-0 items-center gap-2 border-b border-[var(--color-line)] px-3">
        <span
          aria-hidden
          className="grid size-5 shrink-0 place-items-center rounded-[3px] bg-[var(--color-ink)] font-mono text-[10px] font-bold text-[var(--color-void)]"
        >
          F
        </span>
        {!collapsed && (
          <span className="truncate text-[13px] font-medium tracking-tight">
            Command Center
          </span>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden py-2">
        {NAV.map((group) => (
          <div key={group.heading} className="mb-1">
            {!collapsed && (
              <h3 className="px-3 pb-1 pt-2 text-[10px] font-medium uppercase tracking-[0.1em] text-[var(--color-faint)]">
                {group.heading}
              </h3>
            )}
            <ul>
              {group.items.map((item) => (
                <li key={item.slug}>
                  <NavRow
                    item={item}
                    collapsed={collapsed}
                    active={
                      item.href === "/"
                        ? pathname === "/"
                        : Boolean(item.href && pathname.startsWith(item.href))
                    }
                    onBlocked={() => show(item)}
                  />
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      <button
        type="button"
        onClick={() => setCollapsed((v) => !v)}
        aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        className="flex h-9 shrink-0 items-center gap-2.5 border-t border-[var(--color-line)] px-3.5 text-[var(--color-faint)] transition-colors hover:text-[var(--color-muted)]"
      >
        {collapsed ? (
          <PanelLeftOpen className="size-4 shrink-0" />
        ) : (
          <PanelLeftClose className="size-4 shrink-0" />
        )}
        {!collapsed && <span className="text-xs">Collapse</span>}
      </button>
    </nav>
  );
}

function NavRow({
  item,
  collapsed,
  active,
  onBlocked,
}: {
  item: NavItem;
  collapsed: boolean;
  active: boolean;
  onBlocked: () => void;
}) {
  const Icon = item.icon;
  const base =
    "group relative flex h-8 w-full items-center gap-2.5 px-3.5 text-[13px] transition-colors";

  // Present but not navigable. Rendering it as a dead link would be worse:
  // the operator learns the section exists and why it does not work yet.
  if (!item.href) {
    return (
      <button
        type="button"
        onClick={onBlocked}
        title={collapsed ? `${item.label} — not yet available` : undefined}
        className={`${base} cursor-help text-[var(--color-faint)] hover:bg-[var(--color-raised)] hover:text-[var(--color-muted)]`}
      >
        <Icon className="size-4 shrink-0" strokeWidth={1.75} />
        {!collapsed && (
          <>
            <span className="truncate">{item.label}</span>
            <span className="ml-auto font-mono text-[9px] uppercase tracking-wider text-[var(--color-faint)] opacity-60">
              {item.blockedBy}
            </span>
          </>
        )}
      </button>
    );
  }

  return (
    <Link
      href={item.href}
      title={collapsed ? item.label : undefined}
      aria-current={active ? "page" : undefined}
      className={`${base} ${
        active
          ? "bg-[var(--color-raised)] text-[var(--color-ink)]"
          : "text-[var(--color-muted)] hover:bg-[var(--color-raised)] hover:text-[var(--color-ink)]"
      }`}
    >
      {active && (
        <span
          aria-hidden
          className="absolute left-0 top-1/2 h-4 w-[2px] -translate-y-1/2 rounded-r bg-[var(--color-ink)]"
        />
      )}
      <Icon className="size-4 shrink-0" strokeWidth={1.75} />
      {!collapsed && <span className="truncate">{item.label}</span>}
    </Link>
  );
}
