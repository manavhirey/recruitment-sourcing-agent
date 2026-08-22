import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { HttpResponse, http } from "msw"
import { describe, expect, it } from "vitest"

import { CandidateDirectory } from "@/components/candidates/CandidateDirectory"
import { server } from "@/tests/setup"

describe("CandidateDirectory", () => {
  it("shows only authorized job history and no hidden total", async () => {
    const candidateId = "00000000-0000-4000-8000-000000000601"
    server.use(
      http.get("/api/bff/candidates", () => HttpResponse.json({
        items: [{
          id: candidateId,
          name: "Priya Sharma",
          current_title: "Senior Product Manager",
          current_company: "PayFlow",
          location: "New York",
          industry_codes: ["technology.fintech"],
          job_ids: ["00000000-0000-4000-8000-000000000101"],
          updated_at: "2026-08-16T00:00:00Z",
        }],
        next_cursor: null,
      })),
      http.get(`/api/bff/candidates/${candidateId}/jobs`, () => HttpResponse.json([{
        job_candidate_id: "00000000-0000-4000-8000-000000000501",
        job_id: "00000000-0000-4000-8000-000000000101",
        job_title: "Product Manager",
        classification: "main",
        score: 92,
        stage: "Reviewed",
        updated_at: "2026-08-16T00:00:00Z",
      }])),
    )
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <CandidateDirectory initialPage={{ items: [], next_cursor: null }} />
      </QueryClientProvider>,
    )

    await userEvent.type(screen.getByLabelText("Search candidates"), "Priya")
    await userEvent.click(screen.getByRole("button", { name: "Search" }))
    expect(window.location.search).toBe("")
    expect(window.location.href).not.toContain("Priya")
    await userEvent.click(await screen.findByRole("button", { name: /Priya Sharma/ }))
    expect(await screen.findByText("Product Manager")).toBeVisible()
    expect(screen.queryByText(/total/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/other tenant/i)).not.toBeInTheDocument()
  })
})
