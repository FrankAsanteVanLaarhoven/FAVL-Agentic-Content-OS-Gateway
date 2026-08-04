import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // The console holds a gateway token server-side; nothing about the
  // deployment should leak through response headers.
  poweredByHeader: false,
  experimental: { optimizePackageImports: ["lucide-react"] },
};

export default config;
