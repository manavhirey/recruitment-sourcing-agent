import { describe, expect, it, vi } from "vitest"

import { ApiError } from "@/lib/api"
import { membershipValidatedTenantOptions } from "@/lib/tenant-options"

describe("tenant option disclosure boundary", () => {
  it("returns only options whose backend membership was authenticated", async () => {
    const allowed = "00000000-0000-4000-8000-000000000001"
    const hidden = "00000000-0000-4000-8000-000000000002"
    const validateMembership = vi.fn(async (tenantId: string) => {
      if (tenantId === hidden) throw new ApiError(404, "membership_not_found")
    })

    const result = await membershipValidatedTenantOptions(
      [
        { id: allowed, name: "Visible agency" },
        { id: hidden, name: "Must stay private" },
      ],
      validateMembership,
    )

    expect(result).toEqual([{ id: allowed, name: "Visible agency" }])
    expect(validateMembership).toHaveBeenCalledTimes(2)
  })

  it.each([
    new ApiError(401, "unauthenticated"),
    new ApiError(503, "api_unavailable"),
  ])("preserves authentication and availability failures", async (failure) => {
    await expect(
      membershipValidatedTenantOptions(
        [{ id: "00000000-0000-4000-8000-000000000001", name: "Agency" }],
        async () => { throw failure },
      ),
    ).rejects.toBe(failure)
  })
})
