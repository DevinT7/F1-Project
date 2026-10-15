import { defineConfig, devices } from "@playwright/test";

/**
 * Phase 9: the same e2e specs against the docker-compose stack (Postgres, Redis, S3, worker,
 * production frontend image) with its seeded data, instead of the in-process fixture backend.
 * Start the stack first (docker-compose.e2e.yml) and point E2E_BASE_URL at its frontend.
 */
export default defineConfig({
  testDir: "./e2e",
  testIgnore: ["tire-screenshots.spec.ts"],
  timeout: 90_000,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  use: { baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000", trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1000 } } }],
});
