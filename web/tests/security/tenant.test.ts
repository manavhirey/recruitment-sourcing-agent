import { describe, expect, it } from "vitest"

import {
  allowedTenantOptions,
  assertMutationOrigin,
  signTenantSelection,
  verifyTenantSelection,
} from "@/lib/tenant"

const secret = "a-production-strength-auth-secret-value"
const tenantId = "00000000-0000-4000-8000-000000000001"

describe("selected tenant boundary", () => {
  it("signs an expiring domain-separated tenant cookie and rejects tampering", () => {
    const signed = signTenantSelection(tenantId, secret, 1_000)

    expect(verifyTenantSelection(signed, secret, 1_001)).toBe(tenantId)
    expect(verifyTenantSelection(`${signed.slice(0, -1)}x`, secret, 1_001)).toBeNull()
    expect(verifyTenantSelection(signed, secret, 1_000 + 60 * 60 * 24 * 8)).toBeNull()
  })

  it("accepts only same-origin mutation requests", () => {
    expect(() =>
      assertMutationOrigin(
        new Request("https://sourcing.example.com/api/tenant", {
          method: "POST",
          headers: {
            Origin: "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
          },
        }),
        "https://sourcing.example.com",
      ),
    ).toThrow("invalid_request_origin")
  })

  it("offers only verified OIDC hints or server-configured tenant IDs", () => {
    const options = allowedTenantOptions(
      [
        { id: tenantId, name: "Northstar Search" },
        { id: "not-a-uuid", name: "Invalid" },
      ],
      JSON.stringify([
        {
          id: "00000000-0000-4000-8000-000000000002",
          name: "Configured Agency",
        },
      ]),
    )

    expect(options).toEqual([
      { id: tenantId, name: "Northstar Search" },
      {
        id: "00000000-0000-4000-8000-000000000002",
        name: "Configured Agency",
      },
    ])
  })
})
