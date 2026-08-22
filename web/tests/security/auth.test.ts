import { describe, expect, it } from "vitest"

import {
  buildAuthOptions,
  publicSession,
  serverAccessToken,
  withProviderTokens,
} from "@/lib/auth-config"

const validEnvironment = {
  AUTH_SECRET: "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
  AUTH_URL: "https://sourcing.example.com",
  OIDC_ISSUER: "https://identity.example.com/",
  OIDC_AUDIENCE: "https://api.sourcing.example.com",
  OIDC_CLIENT_ID: "sourcing-web",
  OIDC_CLIENT_SECRET: "oidc-client-secret",
  NODE_ENV: "production",
}

describe("Auth.js configuration", () => {
  it("fails closed when required OIDC or encryption configuration is missing", () => {
    expect(() => buildAuthOptions({ NODE_ENV: "production" })).toThrow(
      "authentication_configuration_invalid",
    )
  })

  it("uses a generic OIDC provider and secure encrypted cookie settings", () => {
    const options = buildAuthOptions(validEnvironment)

    expect(options.providers).toHaveLength(1)
    expect(options.providers[0]).toMatchObject({
      id: "oidc",
      type: "oidc",
      wellKnown:
        "https://identity.example.com/.well-known/openid-configuration",
      authorization: {
        params: {
          scope: "openid email profile",
          resource: "https://api.sourcing.example.com",
        },
      },
    })
    expect(options.session?.strategy).toBe("jwt")
    expect(options.cookies?.sessionToken?.options).toMatchObject({
      httpOnly: true,
      sameSite: "lax",
      secure: true,
    })
  })

  it("adds an explicit audience parameter only for compatible providers", () => {
    const options = buildAuthOptions({
      ...validEnvironment,
      OIDC_AUDIENCE_COMPATIBILITY: "true",
    })

    expect(options.providers[0]).toMatchObject({
      authorization: {
        params: {
          resource: "https://api.sourcing.example.com",
          audience: "https://api.sourcing.example.com",
        },
      },
    })
  })

  it("keeps the provider access token in the encrypted JWT and redacts the public session", () => {
    const token = withProviderTokens(
      { sub: "oidc-user" },
      {
        access_token: "access-token-must-stay-server-side",
        refresh_token: "refresh-token-must-stay-server-side",
        expires_at: 4_000_000_000,
      },
    )
    const session = publicSession({
      user: { name: "Agency User", email: "user@example.com" },
      expires: "2099-01-01T00:00:00.000Z",
    })

    expect(token.providerAccessToken).toBe(
      "access-token-must-stay-server-side",
    )
    expect(token).not.toHaveProperty("providerRefreshToken")
    expect(JSON.stringify(session)).not.toContain("access-token")
    expect(JSON.stringify(session)).not.toContain("refresh-token")
    expect(session.user?.email).toBe("user@example.com")
  })

  it("fails closed when the server-only provider token is missing or expired", () => {
    expect(() => serverAccessToken({}, 2_000_000)).toThrow("unauthenticated")
    expect(() =>
      serverAccessToken({ providerAccessToken: "expiry-missing" }, 2_000_000),
    ).toThrow("unauthenticated")
    expect(() =>
      serverAccessToken(
        { providerAccessToken: "expired-token", providerExpiresAt: 1_999 },
        2_000_000,
      ),
    ).toThrow("unauthenticated")
    expect(
      serverAccessToken(
        { providerAccessToken: "server-token", providerExpiresAt: 2_001 },
        2_000_000,
      ),
    ).toBe("server-token")
  })

  it("rejects an OIDC callback that omits access-token expiry", () => {
    expect(() =>
      withProviderTokens(
        { sub: "oidc-user" },
        { access_token: "server-token" },
      ),
    ).toThrow("oidc_callback_invalid")
  })
})
