import { defineConfig, devices } from "@playwright/test";

// Two web servers: (1) the Symphony fixture backend on :7958 (real
// orchestrator + bridge + FastAPI, seeded with deterministic issues),
// and (2) the Vite dev server on :5173 which proxies /api → :7958.
//
// We point the test baseURL at Vite's port so the SPA + the API share the
// same origin during E2E (matches the dev workflow that humans use).

const PYTHON =
  process.env.SYMPHONY_PYTHON ?? "../.venv/bin/python";

export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.spec\.ts$/,
  fullyParallel: false, // shared backend state
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: "http://localhost:5173",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      // Backend on :7957 — matches the existing Vite proxy target.
      command: `${PYTHON} ./e2e/fixture_server.py --port 7957`,
      port: 7957,
      reuseExistingServer: !process.env.CI,
      stdout: "pipe",
      stderr: "pipe",
      timeout: 30_000,
    },
    {
      command: "npm run dev -- --port 5173",
      port: 5173,
      reuseExistingServer: !process.env.CI,
      stdout: "ignore",
      stderr: "pipe",
      timeout: 30_000,
    },
  ],
});
