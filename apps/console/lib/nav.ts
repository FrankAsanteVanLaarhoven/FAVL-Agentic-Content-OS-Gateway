import type { LucideIcon } from "lucide-react";
import {
  Activity,
  Blocks,
  BookLock,
  Boxes,
  BrainCircuit,
  Cpu,
  FileClock,
  LayoutGrid,
  Network,
  Rocket,
  ScrollText,
  Settings,
  ShieldCheck,
  Workflow,
} from "lucide-react";

/**
 * The full platform shape, including what does not exist yet.
 *
 * Sections without a backing API are listed and disabled rather than hidden.
 * A console that looks complete is a worse lie than one that admits gaps —
 * people trust pixels more than they trust a README. `blockedBy` names the
 * milestone that will deliver each one, so the nav doubles as a live roadmap.
 */

export type NavItem = {
  slug: string;
  label: string;
  icon: LucideIcon;
  href?: string;
  blockedBy?: string;
  reason?: string;
};

export type NavGroup = { heading: string; items: NavItem[] };

export const NAV: NavGroup[] = [
  {
    heading: "Operate",
    items: [
      { slug: "workspace", label: "Workspace", icon: LayoutGrid, href: "/" },
      { slug: "agents", label: "Agents", icon: BrainCircuit, href: "/agents" },
      {
        slug: "workflows",
        label: "Workflows",
        icon: Workflow,
        blockedBy: "M2",
        reason:
          "No workflow engine yet. Agents currently fan out to connectors directly; there is no DAG, no checkpointing and no compensation logic to render.",
      },
      {
        slug: "connectors",
        label: "Connectors",
        icon: Blocks,
        href: "/connectors",
      },
      {
        slug: "mcp",
        label: "MCP",
        icon: Network,
        blockedBy: "M1.5",
        reason:
          "MCP servers are not yet modelled as governed connectors. Registration, discovery, tool inventory and schema digests do not exist.",
      },
      {
        slug: "models",
        label: "Models",
        icon: Cpu,
        blockedBy: "M2",
        reason:
          "No model router. Provider routing, token accounting and fallback chains are unimplemented, so there is nothing to configure here.",
      },
      {
        slug: "memory",
        label: "Memory",
        icon: Boxes,
        blockedBy: "M2",
        reason:
          "Memory scopes (session, user, team, org, agent, workflow) have no storage layer behind them yet.",
      },
    ],
  },
  {
    heading: "Govern",
    items: [
      {
        slug: "policies",
        label: "Policies",
        icon: BookLock,
        blockedBy: "M3",
        reason:
          "The policy engine does not exist. Connector-level allowlists and SSRF rules are enforced in the runtime, not by a central evaluator.",
      },
      {
        slug: "security",
        label: "Security",
        icon: ShieldCheck,
        blockedBy: "M1.7",
        reason:
          "Identity is a development Keycloak realm. There is no credential lifecycle, MFA, federation or managed secret store to administer.",
      },
      {
        slug: "audit",
        label: "Audit",
        icon: FileClock,
        href: "/audit",
      },
    ],
  },
  {
    heading: "Observe",
    items: [
      {
        slug: "observability",
        label: "Observability",
        icon: Activity,
        href: "/observability",
      },
      {
        slug: "deployments",
        label: "Deployments",
        icon: Rocket,
        blockedBy: "M1.7",
        reason:
          "Deployment is a local docker compose stack. There are no environments, releases or rollbacks to show.",
      },
      {
        slug: "marketplace",
        label: "Marketplace",
        icon: ScrollText,
        blockedBy: "M4",
        reason:
          "Agent and connector marketplaces are Phase 4. No publishing, versioning or installation flow exists.",
      },
      { slug: "settings", label: "Settings", icon: Settings, href: "/settings" },
    ],
  },
];

export const ALL_ITEMS = NAV.flatMap((g) => g.items);
export const LIVE_ITEMS = ALL_ITEMS.filter((i) => i.href);
export const BLOCKED_ITEMS = ALL_ITEMS.filter((i) => !i.href);
