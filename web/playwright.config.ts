import { defineConfig, devices } from "@playwright/test"

Reflect.deleteProperty(process.env, "NO_COLOR")

export const e2eAuthSecret = "x8V1qM3rT6yB9nC2pL5sF7hJ0kD4wZ6aQ8eR1tY3uI5oP7gH"

const serverEnvironment = {
  ...process.env,
  API_BASE_URL: "http://127.0.0.1:8001",
  AUTH_SECRET: e2eAuthSecret,
  AUTH_URL: "http://127.0.0.1:3000",
  OIDC_ISSUER: "http://127.0.0.1:8001/oidc",
  OIDC_AUDIENCE: "http://127.0.0.1:8001/api",
  OIDC_CLIENT_ID: "e2e-client",
  OIDC_CLIENT_SECRET: "e2e-client-credential",
  TENANT_OPTIONS: JSON.stringify([
    { id: "00000000-0000-4000-8000-000000000001", name: "E2E Agency" },
  ]),
}

export default defineConfig({
  testDir: "./e2e",
  testIgnore: "production-preview.spec.ts",
  fullyParallel: true,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: "html",
  webServer: [
    {
      command: "../backend/.venv/bin/uvicorn tests.e2e_task13_api:app --app-dir ../backend --host 127.0.0.1 --port 8001",
      url: "http://127.0.0.1:8001/health/ready",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: "npm run dev -- --hostname 127.0.0.1",
      env: { ...serverEnvironment, ENABLE_DEV_PREVIEW: "true" },
      url: "http://127.0.0.1:3000/dev-preview",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
  use: {
    baseURL: "http://127.0.0.1:3000",
    trace: "on-first-retry",
  },
  projects: [
    { name: "desktop-chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile-chromium", use: { ...devices["Pixel 7"] } },
  ],
})
