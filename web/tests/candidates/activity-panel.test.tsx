import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import { HttpResponse, http } from "msw"
import { describe, expect, it } from "vitest"

import { ActivityPanel } from "@/components/candidates/ActivityPanel"
import { server } from "@/tests/setup"

describe("ActivityPanel", () => {
  it("renders a run-only allowlisted event without waiting for a disabled candidate query", async () => {
    const runId = "00000000-0000-4000-8000-000000000301"
    server.use(http.get(`/api/bff/runs/${runId}/activity`, () => HttpResponse.json([{
      id: "00000000-0000-4000-8000-000000000b01",
      action: "sourcing_run.usage_budget_exhausted",
      created_at: "2026-08-16T00:00:00Z",
      payload: { provider_error: "secret", token: "never render" },
    }])))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><ActivityPanel runId={runId} /></QueryClientProvider>)

    expect(await screen.findByText("Usage budget reached")).toBeVisible()
    expect(screen.queryByText(/secret|token/i)).not.toBeInTheDocument()
  })

  it("uses candidate activity as the single source for candidate actions", async () => {
    const runId = "00000000-0000-4000-8000-000000000301"
    const jobCandidateId = "00000000-0000-4000-8000-000000000501"
    server.use(
      http.get(`/api/bff/runs/${runId}/activity`, () => HttpResponse.json([{
        id: "00000000-0000-4000-8000-000000000b01",
        action: "candidate.note_added",
        summary: null,
        created_at: "2026-08-16T00:00:00.001Z",
      }])),
      http.get(`/api/bff/job-candidates/${jobCandidateId}/activity`, () => HttpResponse.json({
        items: [{
          id: "00000000-0000-4000-8000-000000000b02",
          action: "candidate.note_added",
          created_at: "2026-08-16T00:00:00.000Z",
        }],
        next_cursor: null,
      })),
    )
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <ActivityPanel runId={runId} jobCandidateId={jobCandidateId} />
      </QueryClientProvider>,
    )

    expect(await screen.findAllByText("Note added")).toHaveLength(1)
  })
})
