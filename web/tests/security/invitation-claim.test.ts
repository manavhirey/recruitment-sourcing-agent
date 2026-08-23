import { describe, expect, it, vi } from "vitest"

import {
  captureInvitation,
  claimCapturedInvitation,
  sealInvitationToken,
} from "@/lib/invitation-claim"

const secret = "a-production-strength-auth-secret-value"
const token = "00000000-0000-4000-8000-000000000001.ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopq"
const appUrl = "https://sourcing.example.com"

describe("invitation claim boundary", () => {
  it("moves a valid one-time token into an encrypted HttpOnly cookie and returns token-free", async () => {
    const response = captureInvitation(token, {
      secret,
      environment: "production",
      nowSeconds: 1_000,
    })
    const cookie = response.headers.get("Set-Cookie") ?? ""

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual({ next: "/invite/claim" })
    expect(response.headers.get("Cache-Control")).toBe("private, no-store")
    expect(response.headers.get("Referrer-Policy")).toBe("no-referrer")
    expect(cookie).toContain("HttpOnly")
    expect(cookie).toContain("SameSite=lax")
    expect(cookie).toContain("Max-Age=600")
    expect(cookie).not.toContain(token)
  })

  it("preserves the opaque cookie through sign-in, then clears it after a stable one-time claim", async () => {
    const sealed = sealInvitationToken(token, secret, 1_000)
    const claim = vi.fn().mockResolvedValue({ tenant_id: "00000000-0000-4000-8000-000000000001" })
    const selectTenant = vi.fn()
    const signedOut = await claimCapturedInvitation(
      new Request(`${appUrl}/invite/claim`),
      {
        secret,
        environment: "production",
        nowSeconds: 1_001,
        readCookie: () => sealed,
        hasSession: async () => false,
        claim,
        selectTenant,
      },
    )
    expect(signedOut.headers.get("Location")).toBe(
      `${appUrl}/api/auth/signin?callbackUrl=%2Finvite%2Fclaim`,
    )
    expect(signedOut.headers.get("Set-Cookie")).toBeNull()
    expect(claim).not.toHaveBeenCalled()

    const signedIn = await claimCapturedInvitation(
      new Request(`${appUrl}/invite/claim`),
      {
        secret,
        environment: "production",
        nowSeconds: 1_002,
        readCookie: () => sealed,
        hasSession: async () => true,
        claim,
        selectTenant,
      },
    )
    const firstKey = claim.mock.calls[0]?.[2]
    expect(claim).toHaveBeenCalledWith(
      token,
      "00000000-0000-4000-8000-000000000001",
      expect.any(String),
    )
    expect(firstKey).toBeTruthy()
    expect(selectTenant).toHaveBeenCalledWith("00000000-0000-4000-8000-000000000001")
    expect(signedIn.headers.get("Location")).toBe(`${appUrl}/jobs`)
    expect(signedIn.headers.get("Set-Cookie")).toContain("Max-Age=0")
    expect(`${signedIn.headers.get("Location")}${signedIn.headers.get("Set-Cookie")}`).not.toContain(token)

    await claimCapturedInvitation(
      new Request(`${appUrl}/invite/claim`),
      {
        secret,
        environment: "production",
        nowSeconds: 1_003,
        readCookie: () => sealed,
        hasSession: async () => true,
        claim,
        selectTenant,
      },
    )
    expect(claim.mock.calls[1]?.[2]).toBe(firstKey)
  })

  it("clears expired and failed claims without putting token material in the response", async () => {
    const sealed = sealInvitationToken(token, secret, 1_000)
    const expired = await claimCapturedInvitation(
      new Request(`${appUrl}/invite/claim`),
      {
        secret,
        environment: "production",
        nowSeconds: 1_601,
        readCookie: () => sealed,
        hasSession: async () => true,
        claim: vi.fn(),
        selectTenant: vi.fn(),
      },
    )
    expect(expired.headers.get("Location")).toBe(`${appUrl}/auth/error?error=InvitationInvalid`)
    expect(expired.headers.get("Set-Cookie")).toContain("Max-Age=0")
    expect(JSON.stringify([...expired.headers])).not.toContain(token)

    const failed = await claimCapturedInvitation(
      new Request(`${appUrl}/invite/claim`),
      {
        secret,
        environment: "production",
        nowSeconds: 1_001,
        readCookie: () => sealed,
        hasSession: async () => true,
        claim: vi.fn().mockRejectedValue(new Error("provider body with token")),
        selectTenant: vi.fn(),
      },
    )
    expect(failed.headers.get("Location")).toBe(`${appUrl}/auth/error?error=InvitationInvalid`)
    expect(failed.headers.get("Set-Cookie")).toContain("Max-Age=0")
    expect(JSON.stringify([...failed.headers])).not.toContain(token)
  })
})
