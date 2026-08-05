import { describe, expect, it } from "vitest";
import {
  connectorTone,
  invocationTone,
  type ConnectorStatus,
  type InvocationStatus,
  type Tone,
} from "./types";

const INVOCATION_STATUSES: InvocationStatus[] = [
  "accepted",
  "running",
  "succeeded",
  "failed_retryable",
  "failed_terminal",
  "timed_out",
  "cancelled",
];

const CONNECTOR_STATUSES: ConnectorStatus[] = [
  "draft",
  "enabled",
  "disabled",
  "deletion_requested",
  "deleted",
];

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
