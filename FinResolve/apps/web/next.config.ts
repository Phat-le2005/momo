import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone output only for the Docker image (keeps `next start` working locally).
  output: process.env.NEXT_STANDALONE === "1" ? "standalone" : undefined,
  poweredByHeader: false,
  reactStrictMode: true,
};

export default nextConfig;
