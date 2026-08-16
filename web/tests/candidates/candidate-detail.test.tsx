import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { HttpResponse, http } from "msw"
import { describe, expect, it } from "vitest"

import { CandidateDetail } from "@/components/candidates/CandidateDetail"
import { priyaCandidateFixture } from "@/tests/review-fixtures"
import { server } from "@/tests/setup"

describe("CandidateDetail", () => {
  it("moves focus into the controlled rejection dialog", async () => {
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    render(<QueryClientProvider client={client}><CandidateDetail candidate={priyaCandidateFixture} /></QueryClientProvider>)

    await userEvent.click(screen.getByRole("button", { name: "Reject" }))
    expect(screen.getByRole("dialog", { name: "Reject candidate" })).toBeVisible()
    expect(screen.getByLabelText("Reason")).toHaveFocus()
    await userEvent.selectOptions(screen.getByLabelText("Reason"), "location_mismatch")
    expect(screen.getByRole("button", { name: "Confirm rejection" })).toBeEnabled()

    await userEvent.tab({ shift: true })
    expect(screen.getByRole("button", { name: "Confirm rejection" })).toHaveFocus()
    await userEvent.keyboard("{Escape}")
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Reject" })).toHaveFocus()
  })

  it("reuses one intent key when a safely rolled-back stage change is retried", async () => {
    const keys: string[] = []
    server.use(http.patch(`/api/bff/job-candidates/${priyaCandidateFixture.id}/stage`, ({ request }) => {
      keys.push(request.headers.get("Idempotency-Key") ?? "")
      return keys.length === 1
        ? HttpResponse.json({ code: "conflict" }, { status: 409 })
        : HttpResponse.json({ ...priyaCandidateFixture, stage: "Shortlisted" })
    }))
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    render(<QueryClientProvider client={client}><CandidateDetail candidate={priyaCandidateFixture} /></QueryClientProvider>)

    await userEvent.click(screen.getByRole("button", { name: "Shortlist" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("authoritative candidate state")
    expect(screen.getByText("New")).toBeVisible()
    await userEvent.click(screen.getByRole("button", { name: "Shortlist" }))
    expect(await screen.findByText("Shortlisted")).toBeVisible()
    expect(keys).toHaveLength(2)
    expect(keys[0]).toBeTruthy()
    expect(keys[1]).toBe(keys[0])
  })

  it("offers only the backend-supported transition for a rejected candidate", () => {
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <CandidateDetail candidate={{ ...priyaCandidateFixture, stage: "Rejected", rejection_reason_code: "other" }} />
      </QueryClientProvider>,
    )
    expect(screen.getByRole("button", { name: "Mark Reviewed" })).toBeVisible()
    expect(screen.queryByRole("button", { name: "Shortlist" })).not.toBeInTheDocument()
  })

  it("reconciles authoritative notes when the candidate timestamp is unchanged", () => {
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const { rerender } = render(
      <QueryClientProvider client={client}>
        <CandidateDetail candidate={priyaCandidateFixture} />
      </QueryClientProvider>,
    )

    rerender(
      <QueryClientProvider client={client}>
        <CandidateDetail
          candidate={{
            ...priyaCandidateFixture,
            notes: [
              ...(priyaCandidateFixture.notes ?? []),
              {
                id: "00000000-0000-4000-8000-000000000799",
                job_candidate_id: priyaCandidateFixture.id,
                body: "Authoritative note returned by the API.",
                actor_user_id: "00000000-0000-4000-8000-000000000802",
                created_at: "2026-08-16T12:30:00Z",
                updated_at: "2026-08-16T12:30:00Z",
              },
            ],
          }}
        />
      </QueryClientProvider>,
    )

    expect(screen.getByText("Authoritative note returned by the API.")).toBeVisible()
  })
})
