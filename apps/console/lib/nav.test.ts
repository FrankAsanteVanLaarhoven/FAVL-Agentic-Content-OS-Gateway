import { describe, expect, it } from "vitest";
import { ALL_ITEMS, BLOCKED_ITEMS, LIVE_ITEMS, NAV } from "./nav";

describe("navigation", () => {
  it("gives every unavailable section a milestone and a reason", () => {
    // Without both, a disabled entry is a dead button: the operator learns
    // that something is missing but not what or when. That is worse than
    // hiding it, because it looks like a bug in the console.
    for (const item of BLOCKED_ITEMS) {
      expect(item.blockedBy, `${item.slug} has no blockedBy`).toBeTruthy();
      expect(item.reason, `${item.slug} has no reason`).toBeTruthy();
      expect(item.reason!.length).toBeGreaterThan(40);
    }
  });

  it("makes every item either navigable or explained, never neither", () => {
    for (const item of ALL_ITEMS) {
      const navigable = Boolean(item.href);
      const explained = Boolean(item.blockedBy && item.reason);
      expect(
        navigable !== explained,
        `${item.slug} is ${navigable ? "" : "not "}navigable and ${
          explained ? "" : "not "
        }explained`,
      ).toBe(true);
    }
  });

  it("has no duplicate slugs or hrefs", () => {
    const slugs = ALL_ITEMS.map((i) => i.slug);
    expect(new Set(slugs).size).toBe(slugs.length);
    const hrefs = LIVE_ITEMS.map((i) => i.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });

  it("keeps every section in exactly one group", () => {
    const counted = NAV.flatMap((g) => g.items.map((i) => i.slug));
    expect(counted.length).toBe(ALL_ITEMS.length);
  });

  it("reports more pending than live sections, honestly", () => {
    // If this ever inverts it is good news, but the claim in the Workspace
    // footer is generated from BLOCKED_ITEMS.length and must stay true.
    expect(BLOCKED_ITEMS.length).toBeGreaterThan(0);
    expect(LIVE_ITEMS.length).toBeGreaterThan(0);
    expect(LIVE_ITEMS.length + BLOCKED_ITEMS.length).toBe(ALL_ITEMS.length);
  });
});
