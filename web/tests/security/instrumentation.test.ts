import { afterEach, describe, expect, it, vi } from "vitest"

import { register } from "@/instrumentation"

afterEach(() => {
  vi.unstubAllEnvs()
})

describe("production runtime configuration", () => {
  it("rejects the developer authentication override at server startup", async () => {
    const environment = {
      NODE_ENV: "production",
      ENABLE_DEV_AUTH_OVERRIDE: "true",
      AUTH_SECRET: "A".repeat(43),
      AUTH_URL: "https://sourcing.example.com",
      OIDC_ISSUER: "https://identity.example.com/",
      OIDC_AUDIENCE: "https://api.example.com/",
      API_BASE_URL: "https://api.example.com/",
      OIDC_CLIENT_ID: "sourcing-web",
      OIDC_CLIENT_SECRET: "production-client-credential",
    }
    for (const [name, value] of Object.entries(environment)) {
      vi.stubEnv(name, value)
    }

    await expect(register()).rejects.toThrow("production_configuration_invalid")
  })
})
