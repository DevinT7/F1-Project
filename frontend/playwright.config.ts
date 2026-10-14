import { defineConfig, devices } from "@playwright/test";

// E2E smoke against the real backend serving the Parquet fixture repository
// (2024 R5 Qualifying, VER/PER/ALO). Both servers are started here.
const BACKEND_PORT = 8765;
const FRONTEND_PORT = 3100;
const python = process.platform === "win32" ? ".venv\\Scripts\\python.exe" : ".venv/bin/python";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: 0,
  reporter: [["list"]],
  use: { baseURL: `http://localhost:${FRONTEND_PORT}`, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1000 } } }],
  webServer: [
    {
      command: `${python} -m uvicorn app.main:app --port ${BACKEND_PORT}`,
      cwd: "../backend",
      url: `http://localhost:${BACKEND_PORT}/api/v1/health`,
      // RACEIQ_ENGINEER_FAKE_LLM=inject: the engineer's LLM is a scripted fake that injects an ungrounded
      // number and a fabricated cause into every draft (e2e/engineer.spec.ts asserts neither reaches the DOM)
      env: { RACEIQ_DATA_DIR: "tests/fixtures/ingestion/store", RACEIQ_ENGINEER_FAKE_LLM: "inject" },
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
    {
      // webpack build: Turbopack spawns a separate PostCSS worker that some sandboxed
      // Windows environments refuse to start (0xc0000142); webpack runs PostCSS in-process.
      command: `npx next build --webpack && npx next start -p ${FRONTEND_PORT}`,
      url: `http://localhost:${FRONTEND_PORT}`,
      env: { BACKEND_URL: `http://localhost:${BACKEND_PORT}` },
      reuseExistingServer: !process.env.CI,
      timeout: 240_000,
    },
  ],
});
