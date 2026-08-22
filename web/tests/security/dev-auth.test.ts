import { describe, expect, it } from "vitest"

import {
  DEV_TENANT,
  developmentAuthOverride,
} from "@/lib/dev-auth"

const enabled = {
  ENABLE_DEV_AUTH_OVERRIDE: "true",
  NODE_ENV: "development",
}

describe("developer authentication override", () => {
  it.each(["localhost:3000", "127.0.0.1:3000", "[::1]:3000"])(
    "creates the fixed local identity on %s",
    (host) => {
      const override = developmentAuthOverride(enabled, host, 1_000)

      expect(override).toMatchObject({
        tenantId: DEV_TENANT.id,
        token: {
          providerAccessToken: "e2e-access-token",
          providerExpiresAt: 4_600,
          tenantOptions: [DEV_TENANT],
        },
        session: {
          user: {
            name: "Local Developer",
            email: "developer@example.test",
          },
        },
      })
    },
  )

  it("stays disabled unless explicitly enabled", () => {
    expect(
      developmentAuthOverride({ NODE_ENV: "development" }, "localhost:3000"),
    ).toBeNull()
  })

  it("rejects non-loopback hosts and production", () => {
    expect(developmentAuthOverride(enabled, "dev.example.com")).toBeNull()
    expect(
      developmentAuthOverride(
        { ...enabled, NODE_ENV: "production" },
        "localhost:3000",
      ),
    ).toBeNull()
  })

  it.each([
    "evil.example@localhost:3000",
    "localhost/path",
    "localhost?override=true",
    "localhost#override",
    "2130706433:3000",
    "127.0.0.1:99999",
  ])("rejects malformed or aliased Host value %s", (host) => {
    expect(developmentAuthOverride(enabled, host)).toBeNull()
  })

  it("does not trust forwarded host data", () => {
    expect(developmentAuthOverride(enabled, null)).toBeNull()
  })
})
