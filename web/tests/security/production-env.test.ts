import { assertProductionEnvironment } from "@/production-env"

const validEnvironment = {
  AUTH_SECRET: "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
  AUTH_URL: "https://sourcing.example.com",
  API_BASE_URL: "https://api.sourcing.example.com",
  OIDC_ISSUER: "https://identity.example.com",
  OIDC_AUDIENCE: "https://api.sourcing.example.com",
  OIDC_CLIENT_ID: "sourcing-web",
  OIDC_CLIENT_SECRET: "oidc-client-secret",
}

describe("production environment", () => {
  it("fails closed without every server-side authentication and API setting", () => {
    expect(() => assertProductionEnvironment({})).toThrow(
      "production_configuration_invalid",
    )
  })

  it("accepts secure URLs and a strong encryption secret without exposing values", () => {
    expect(() => assertProductionEnvironment(validEnvironment)).not.toThrow()
    expect(() =>
      assertProductionEnvironment({ ...validEnvironment, API_BASE_URL: "http://api.example.com" }),
    ).toThrow("production_configuration_invalid")
  })

  it("requires the API audience shared with backend token verification", () => {
    expect(() =>
      assertProductionEnvironment({
        ...validEnvironment,
        OIDC_AUDIENCE: undefined,
      }),
    ).toThrow("production_configuration_invalid")
    expect(() =>
      assertProductionEnvironment({
        ...validEnvironment,
        OIDC_AUDIENCE_COMPATIBILITY: "sometimes",
      }),
    ).toThrow("production_configuration_invalid")
  })

  it("rejects the public sample placeholder as an encryption key", () => {
    expect(() => assertProductionEnvironment({
      ...validEnvironment,
      AUTH_SECRET: "replace-with-at-least-32-random-characters",
    })).toThrow("production_configuration_invalid")
  })

  it("rejects the developer authentication override in production", () => {
    expect(() => assertProductionEnvironment({
      ...validEnvironment,
      ENABLE_DEV_AUTH_OVERRIDE: "true",
    })).toThrow("production_configuration_invalid")
  })
})
