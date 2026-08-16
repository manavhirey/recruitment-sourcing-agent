import { defineConfig, devices } from "@playwright/test"

Reflect.deleteProperty(process.env, "NO_COLOR")

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: "html",
  webServer: {
    command: "ENABLE_DEV_PREVIEW=true npm run dev -- --hostname 127.0.0.1",
    url: "http://127.0.0.1:3000/dev-preview",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
  use: {
    baseURL: "http://127.0.0.1:3000",
    trace: "on-first-retry",
  },
  projects: [
    { name: "desktop-chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile-chromium", use: { ...devices["Pixel 7"] } },
  ],
})
