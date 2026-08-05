import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./", import.meta.url)),
      // See test/server-only-stub.ts.
      "server-only": fileURLToPath(
        new URL("./test/server-only-stub.ts", import.meta.url),
      ),
    },
  },
  test: {
    // Only pure logic is unit-tested here. Component behaviour is covered by
    // the live checks against the running stack, which exercise the real
    // gateway rather than a mock of it.
    include: ["lib/**/*.test.ts"],
    environment: "node",
  },
});
