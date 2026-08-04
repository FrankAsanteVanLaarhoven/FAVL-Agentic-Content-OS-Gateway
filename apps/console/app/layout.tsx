import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { Providers } from "@/components/providers";
import { Sidebar } from "@/components/sidebar";
import { StatusBar } from "@/components/status-bar";
import { CommandPalette } from "@/components/command-palette";
import { UnavailableProvider } from "@/components/unavailable";

export const metadata: Metadata = {
  title: "FAVL Command Center",
  description: "Operational console for the FAVL Enterprise Gateway",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="h-dvh overflow-hidden">
        <Providers>
          <UnavailableProvider>
            <div className="flex h-dvh flex-col">
              <div className="flex min-h-0 flex-1">
                <Sidebar />
                <div className="flex min-w-0 flex-1 flex-col">
                  <header className="flex h-11 shrink-0 items-center gap-3 border-b border-[var(--color-line)] bg-[var(--color-surface)] px-3">
                    <CommandPalette />
                    <span className="ml-auto flex items-center gap-2 text-xs text-[var(--color-muted)]">
                      <span
                        aria-hidden
                        className="grid size-5 place-items-center rounded-full border border-[var(--color-line)] bg-[var(--color-raised)] font-mono text-[10px]"
                      >
                        D
                      </span>
                      demo
                    </span>
                  </header>
                  <main className="min-h-0 flex-1 overflow-hidden bg-[var(--color-void)]">
                    {children}
                  </main>
                </div>
              </div>
              <StatusBar />
            </div>
          </UnavailableProvider>
        </Providers>
      </body>
    </html>
  );
}
