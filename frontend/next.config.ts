import { withSentryConfig } from "@sentry/nextjs/config";
import type { NextConfig } from "next";

// Same idea as the old Vite dev proxy: the browser calls /api/* on this origin and Next
// forwards it to FastAPI, so there are no CORS issues. Point BACKEND_URL at a deployed
// backend for prod (the deploy workflow sets it, and NEXT_PUBLIC_API_URL, to each environment's API).
const backend = process.env.BACKEND_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const config: NextConfig = {
  reactStrictMode: true,
  // a self-contained server bundle for the container image (frontend/Dockerfile)
  output: "standalone",
  // next dev would otherwise (re)write an AGENTS.md into the repo on every start
  agentRules: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

// Sentry (Phase 9): source maps upload only when SENTRY_AUTH_TOKEN (+ SENTRY_ORG / SENTRY_PROJECT) is set
// in the build environment (the deploy workflow); otherwise the build is unchanged and nothing is sent.
export default withSentryConfig(config, {
  org: process.env.SENTRY_ORG,
  project: process.env.SENTRY_PROJECT,
  authToken: process.env.SENTRY_AUTH_TOKEN,
  sourcemaps: { disable: !process.env.SENTRY_AUTH_TOKEN },
  release: { create: Boolean(process.env.SENTRY_AUTH_TOKEN) },
  silent: !process.env.CI,
  telemetry: false,
});
