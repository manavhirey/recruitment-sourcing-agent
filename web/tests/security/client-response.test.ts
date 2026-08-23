import { vi } from "vitest"

import { reauthenticateExpiredSession, responseJson } from "@/lib/client-response"

describe("client response boundary", () => {
  it("routes an expired BFF session to sign-in with a same-origin callback", async () => {
    window.history.replaceState({}, "", "/jobs")
    const router = { replace: vi.fn() }
    let failure: unknown
    try {
      await responseJson(
        new Response(JSON.stringify({ code: "unauthenticated" }), { status: 401 }),
      )
    } catch (error) {
      failure = error
    }

    expect(reauthenticateExpiredSession(failure, router)).toBe(true)
    expect(router.replace).toHaveBeenCalledWith(
      "/api/auth/signin?callbackUrl=%2Fjobs",
    )
  })

  it("bounds error bodies before parsing", async () => {
    await expect(
      responseJson(new Response("x".repeat(8 * 1024 + 1), { status: 502 })),
    ).rejects.toEqual(expect.objectContaining({
      code: "response_too_large",
      status: 502,
    }))
  })
})
