import { defineConfig, devices } from "@playwright/test"

Reflect.deleteProperty(process.env, "NO_COLOR")

const productionEnvironment = {
  ...process.env,
  API_BASE_URL: "https://api.e2e.invalid",
  AUTH_SECRET: "v9Q2mN5tR8yC1pF4hJ7kL0wZ3aD6eG9sU2iO5bV8xM1qT4rY",
  AUTH_URL: "https://sourcing.e2e.invalid",
  ENABLE_DEV_PREVIEW: "true",
  OIDC_ISSUER: "https://identity.e2e.invalid/",
  OIDC_AUDIENCE: "https://api.e2e.invalid/",
  OIDC_CLIENT_ID: "production-preview-smoke",
  OIDC_CLIENT_SECRET: "production-preview-credential",
}

export default defineConfig({
  testDir: "./e2e",
  testMatch: "production-preview.spec.ts",
  forbidOnly: true,
  reporter: "list",
  webServer: {
    command: "npm run build && npm run start -- --hostname 127.0.0.1 --port 3100",
    env: productionEnvironment,
    url: "http://127.0.0.1:3100/api/auth/session",
    reuseExistingServer: false,
    timeout: 180_000,
  },
  use: {
    baseURL: "http://127.0.0.1:3100",
    trace: "on-first-retry",
  },
  projects: [
    { name: "production-chromium", use: { ...devices["Desktop Chrome"] } },
  ],
})
