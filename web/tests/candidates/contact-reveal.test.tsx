import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { HttpResponse, http } from "msw"
import { describe, expect, it, vi } from "vitest"

import { ContactReveal } from "@/components/candidates/ContactReveal"
import { server } from "@/tests/setup"

const contact = {
  id: "00000000-0000-4000-8000-000000000701",
  kind: "email",
  classification: "work",
  verification_state: "verified",
  masked_value: "••••@••••",
  expires_at: "2027-01-01T00:00:00Z",
}

describe("ContactReveal", () => {
  it("keeps plaintext in component memory and clears it on candidate change", async () => {
    server.use(
      http.post(`/api/bff/contact-points/${contact.id}/reveal`, () =>
        HttpResponse.json({ id: contact.id, value: "priya@example.test" }, {
          headers: { "Cache-Control": "no-store" },
        }),
      ),
    )
    const { rerender } = render(
      <ContactReveal candidateId="00000000-0000-4000-8000-000000000601" contacts={[contact]} />,
    )
    await userEvent.click(screen.getByRole("button", { name: "Reveal work email" }))
    expect(await screen.findByText("priya@example.test")).toBeVisible()

    rerender(
      <ContactReveal candidateId="00000000-0000-4000-8000-000000000602" contacts={[contact]} />,
    )
    expect(screen.queryByText("priya@example.test")).not.toBeInTheDocument()
  })

  it("clears revealed plaintext after the bounded display timeout", async () => {
    vi.useFakeTimers()
    server.use(
      http.post(`/api/bff/contact-points/${contact.id}/reveal`, () =>
        HttpResponse.json({ id: contact.id, value: "priya@example.test" }),
      ),
    )
    render(<ContactReveal candidateId="00000000-0000-4000-8000-000000000601" contacts={[contact]} />)
    screen.getByRole("button", { name: "Reveal work email" }).click()
    await vi.runAllTimersAsync()
    expect(screen.queryByText("priya@example.test")).not.toBeInTheDocument()
    vi.useRealTimers()
  })

  it("offers paid enrichment only when the backend marks the candidate eligible", () => {
    const candidateId = "00000000-0000-4000-8000-000000000601"
    const { rerender } = render(
      <ContactReveal
        candidateId={candidateId}
        contacts={[]}
        runCandidateId="00000000-0000-4000-8000-000000000611"
        enrichmentEligible={false}
        estimatedEnrichmentCredits={null}
      />,
    )
    expect(screen.queryByRole("button", { name: "Enrich contact" })).not.toBeInTheDocument()

    rerender(
      <ContactReveal
        candidateId={candidateId}
        contacts={[]}
        runCandidateId="00000000-0000-4000-8000-000000000611"
        enrichmentEligible
        estimatedEnrichmentCredits={9}
      />,
    )
    expect(screen.getByText("Estimated cost: up to 9 provider credits.")).toBeVisible()
    expect(screen.getByRole("button", { name: "Enrich contact" })).toBeVisible()
  })
})
