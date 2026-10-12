import type { NextConfig } from "next";

// Same idea as the old Vite dev proxy: the browser calls /api/* on this origin and Next
// forwards it to FastAPI, so there are no CORS issues. Point BACKEND_URL at a deployed
// backend for prod.
const backend = process.env.BACKEND_URL ?? "http://localhost:8000";

const config: NextConfig = {
  reactStrictMode: true,
  // next dev would otherwise (re)write an AGENTS.md into the repo on every start
  agentRules: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

export default config;
