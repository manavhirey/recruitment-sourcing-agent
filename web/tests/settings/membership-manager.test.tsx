import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { HttpResponse, http } from "msw"
import { afterEach, describe, expect, it, vi } from "vitest"

import { MembershipManager } from "@/components/layout/MembershipManager"
import { server } from "@/tests/setup"

const member = {
  membership_id: "00000000-0000-4000-8000-000000000801",
  user_id: "00000000-0000-4000-8000-000000000802",
  email: "recruiter@example.test",
  display_name: "Recruiter",
  role: "recruiter" as const,
  allowed_client_ids: [],
  active: true,
}

afterEach(() => vi.restoreAllMocks())

describe("MembershipManager", () => {
  it("hides membership controls from recruiters", () => {
    render(<MembershipManager role="recruiter" members={[]} clients={[]} />)
    expect(screen.queryByRole("button", { name: /invite/i })).not.toBeInTheDocument()
    expect(screen.getByText("Only agency owners and admins can manage membership.")).toBeVisible()
  })

  it("creates an ephemeral invitation and never submits email verification claims", async () => {
    let requestBody: unknown
    server.use(
      http.post("/api/bff/membership-invitations", async ({ request }) => {
        requestBody = await request.json()
        return HttpResponse.json({
          invitation_id: "00000000-0000-4000-8000-000000000901",
          token: "one-time-secret",
          expires_at: "2026-08-23T00:00:00Z",
        }, { headers: { "Cache-Control": "no-store" } })
      }),
    )
    render(<MembershipManager role="owner" members={[member]} clients={[]} />)
    await userEvent.type(screen.getByLabelText("Invitation email"), "new@example.test")
    await userEvent.click(screen.getByRole("button", { name: "Create invitation" }))
    expect(await screen.findByText(/one-time-secret/)).toBeVisible()
    expect(requestBody).toEqual({ email: "new@example.test", role: "recruiter" })
    expect(JSON.stringify(requestBody)).not.toContain("email_verified")
  })

  it("cancels the previous expiry timer before showing a newer invitation", async () => {
    const realSetTimeout = globalThis.setTimeout
    const realClearTimeout = globalThis.clearTimeout
    const cleared: number[] = []
    let expiryId = 10_000
    vi.spyOn(globalThis, "setTimeout").mockImplementation(((callback: () => void, delay?: number) => {
      if ((delay ?? 0) > 60_000) {
        expiryId += 1
        return expiryId as unknown as ReturnType<typeof setTimeout>
      }
      return realSetTimeout(callback, delay)
    }) as typeof setTimeout)
    vi.spyOn(globalThis, "clearTimeout").mockImplementation((timer) => {
      if (typeof timer === "number" && timer >= 10_001) cleared.push(timer)
      else realClearTimeout(timer)
    })
    let issued = 0
    server.use(http.post("/api/bff/membership-invitations", () => {
      issued += 1
      return HttpResponse.json({
        invitation_id: `00000000-0000-4000-8000-00000000090${issued}`,
        token: `one-time-secret-${issued}`,
        expires_at: "2026-08-23T00:00:00Z",
      })
    }))
    render(<MembershipManager role="owner" members={[member]} clients={[]} />)

    await userEvent.type(screen.getByLabelText("Invitation email"), "first@example.test")
    await userEvent.click(screen.getByRole("button", { name: "Create invitation" }))
    expect(await screen.findByText(/one-time-secret-1/)).toBeVisible()
    await userEvent.type(screen.getByLabelText("Invitation email"), "second@example.test")
    await userEvent.click(screen.getByRole("button", { name: "Create invitation" }))
    expect(await screen.findByText(/one-time-secret-2/)).toBeVisible()

    expect(cleared).toContain(10_001)
    expect(screen.getByText(/one-time-secret-2/)).toBeVisible()
  })
})
