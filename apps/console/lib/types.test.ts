import { describe, expect, it } from "vitest";
import {
  connectorTone,
  invocationTone,
  type ConnectorStatus,
  type InvocationStatus,
  type Tone,
} from "./types";

// These lists are hand-written, so they drift. When the registry gained five
// connector states this file still listed five, and the "every status maps to
// a tone" test kept passing while five statuses had no mapping at all — the
// test was exhaustive over the list, not over the type.
//
// `Covers<Union, List>` closes that: it resolves to `true` only when the list
// contains every member of the union, so a status added to the type and not
// to the list fails to compile rather than silently going untested.
type Covers<Union extends string, List extends readonly string[]> =
  Exclude<Union, List[number]> extends never ? true : never;

const INVOCATION_STATUSES = [
  "accepted",
  "running",
  "succeeded",
  "failed_retryable",
  "failed_terminal",
  "timed_out",
  "cancelled",
] as const satisfies readonly InvocationStatus[];

const CONNECTOR_STATUSES = [
  "draft",
  "installed",
  "configured",
  "validated",
  "enabled",
  "disabled",
  "revoked",
  "deletion_requested",
  "archived",
  "deleted",
] as const satisfies readonly ConnectorStatus[];

const _invocationsCovered: Covers<InvocationStatus, typeof INVOCATION_STATUSES> =
  true;
const _connectorsCovered: Covers<ConnectorStatus, typeof CONNECTOR_STATUSES> =
  true;
void _invocationsCovered;
void _connectorsCovered;

const TONES: Tone[] = ["ok", "warn", "err", "info", "idle"];

describe("state tone mapping", () => {
  it("maps every invocation status to a defined tone", () => {
    // A status with no mapping renders `undefined` as a class name, which
    // silently produces an uncoloured dot — a failed invocation that looks
    // idle. The union is exhaustive here so a new status breaks the build.
    for (const status of INVOCATION_STATUSES) {
      expect(TONES, status).toContain(invocationTone(status));
    }
  });

  it("maps every connector status to a defined tone", () => {
    for (const status of CONNECTOR_STATUSES) {
      expect(TONES, status).toContain(connectorTone(status));
    }
  });

  it("never shows a terminal failure as success", () => {
    expect(invocationTone("failed_terminal")).toBe("err");
    expect(invocationTone("cancelled")).toBe("err");
    expect(invocationTone("succeeded")).toBe("ok");
  });

  it("distinguishes retryable failure from terminal failure", () => {
    // An operator triages these differently: one will resolve itself.
    expect(invocationTone("failed_retryable")).not.toBe(
      invocationTone("failed_terminal"),
    );
    expect(invocationTone("timed_out")).toBe("warn");
  });

  it("treats a deleted connector as more severe than a disabled one", () => {
    expect(connectorTone("deleted")).toBe("err");
    expect(connectorTone("deletion_requested")).toBe("warn");
    expect(connectorTone("disabled")).toBe("idle");
    expect(connectorTone("enabled")).toBe("ok");
  });
});
