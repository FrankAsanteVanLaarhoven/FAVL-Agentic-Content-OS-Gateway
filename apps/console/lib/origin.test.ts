import { describe, expect, it } from "vitest";
import { CrossOriginRequest, requireSameOrigin } from "./origin";

function request(headers: Record<string, string>): Request {
  return new Request("https://console.local/api/agents", { headers });
}

describe("same-origin enforcement", () => {
  it("accepts a same-origin fetch", () => {
    expect(() =>
      requireSameOrigin(
        request({ "sec-fetch-site": "same-origin", host: "console.local" }),
      ),
    ).not.toThrow();
  });

  it("rejects a cross-site request", () => {
    // The console's route handlers attach a gateway credential, so a
    // cross-site POST would execute with it — the confused deputy.
    for (const site of ["cross-site", "same-site", "none"]) {
      expect(() =>
        requireSameOrigin(request({ "sec-fetch-site": site })),
      ).toThrow(CrossOriginRequest);
    }
  });

  it("rejects a mismatched Origin when Sec-Fetch-Site is absent", () => {
    expect(() =>
      requireSameOrigin(
        request({ origin: "https://evil.example", host: "console.local" }),
      ),
    ).toThrow(CrossOriginRequest);
  });

  it("accepts a matching Origin when Sec-Fetch-Site is absent", () => {
    expect(() =>
      requireSameOrigin(
        request({ origin: "https://console.local", host: "console.local" }),
      ),
    ).not.toThrow();
  });

  it("refuses when neither signal is present rather than assuming", () => {
    // Failing open here would defeat the whole control for any client that
    // simply omits both headers.
    expect(() => requireSameOrigin(request({}))).toThrow(CrossOriginRequest);
  });
});
