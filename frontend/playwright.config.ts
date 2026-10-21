import { defineConfig, devices } from "@playwright/test";

const externalBaseURL = process.env.STUDIO_E2E_BASE_URL;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: "list",
  use: {
    baseURL: externalBaseURL || "http://127.0.0.1:8847",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop-chromium", use: { ...devices["Desktop Chrome"] } },
    {
      name: "mobile-chromium",
      use: { ...devices["iPhone 13"], browserName: "chromium" },
    },
    { name: "iphone-webkit", use: { ...devices["iPhone 13"] } },
  ],
  webServer: externalBaseURL
    ? undefined
    : {
        command: "python -m backend.demo_server --port 8847",
        cwd: "..",
        url: "http://127.0.0.1:8847/health",
        timeout: 30_000,
        reuseExistingServer: false,
      },
});
