import { defineConfig, devices } from "@playwright/test";

/** Phone first: the owner uses this at 375 px. The app is already built when this runs. */
export default defineConfig({
  testDir: "./e2e",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:3100",
    ...devices["iPhone 13"],
    defaultBrowserType: "chromium",
    viewport: { width: 375, height: 760 },
    permissions: ["clipboard-read", "clipboard-write"],
  },
  webServer: [
    { command: "node e2e/mock-api.mjs", port: 8100, reuseExistingServer: false },
    {
      command: "npx next start --hostname 127.0.0.1 --port 3100",
      port: 3100,
      reuseExistingServer: false,
      env: { HOOPSEDGE_API_URL: "http://127.0.0.1:8100", HOOPSEDGE_API_KEY: "e2e-key" },
    },
  ],
});
