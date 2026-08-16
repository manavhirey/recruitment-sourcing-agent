import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it } from "vitest"
import { HttpResponse, http } from "msw"

import { ReviewWorkspace } from "@/components/candidates/ReviewWorkspace"
import { priyaCandidateFixture, marcusCandidateFixture } from "@/tests/review-fixtures"
import { nearMatchFixture } from "@/tests/review-fixtures"
import { server } from "@/tests/setup"

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <QueryClientProvider client={client}>
      <ReviewWorkspace
        jobId={priyaCandidateFixture.job_id}
        initialCandidates={{ items: [priyaCandidateFixture, marcusCandidateFixture], next_cursor: null }}
        initialSelectedCandidate={priyaCandidateFixture}
        initialNearMatches={{ items: [], next_cursor: null }}
        immutableScorecard={null}
      />
    </QueryClientProvider>,
  )
}

describe("ReviewWorkspace", () => {
  it("shows stored evidence and uncertainty for the selected ranked candidate", async () => {
    renderWorkspace()
    await userEvent.click(screen.getByRole("button", { name: /Priya Sharma.*92/ }))
    expect(screen.getByText("5 years in payments and fintech")).toBeVisible()
    expect(screen.getByText("US market experience is unknown")).toBeVisible()
    expect(screen.getByText("Scorecard version 3")).toBeVisible()
  })

  it("keeps only the selected job-candidate UUID in the URL", async () => {
    renderWorkspace()
    await userEvent.click(screen.getByRole("button", { name: /Marcus Lee.*84/ }))
    expect(window.location.search).toBe(`?candidate=${marcusCandidateFixture.id}`)
    expect(window.location.href).not.toContain("Marcus")
  })

  it("supports arrow-key tabs and focuses detail only after candidate selection", async () => {
    renderWorkspace()
    const initialDetail = screen.getByRole("article", { name: "Priya Sharma" })
    expect(initialDetail).not.toHaveFocus()

    const reviewTab = screen.getByRole("tab", { name: "Review" })
    reviewTab.focus()
    await userEvent.keyboard("{ArrowRight}")
    expect(screen.getByRole("tab", { name: "All Candidates" })).toHaveFocus()
    expect(screen.getByRole("tab", { name: "All Candidates" })).toHaveAttribute("aria-selected", "true")

    await userEvent.click(screen.getByRole("tab", { name: "Review" }))
    await userEvent.click(screen.getByRole("button", { name: /Marcus Lee.*84/ }))
    expect(screen.getByRole("article", { name: "Marcus Lee" })).toHaveFocus()
  })

  it("refreshes near matches when the shared job-candidate query is invalidated", async () => {
    server.use(http.get(`/api/bff/jobs/${priyaCandidateFixture.job_id}/candidates`, ({ request }) => {
      const classification = new URL(request.url).searchParams.get("classification")
      return HttpResponse.json(classification === "near_match"
        ? { items: [nearMatchFixture], next_cursor: null }
        : { items: [priyaCandidateFixture, marcusCandidateFixture], next_cursor: null })
    }))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    render(
      <QueryClientProvider client={client}>
        <ReviewWorkspace
          jobId={priyaCandidateFixture.job_id}
          initialCandidates={{ items: [priyaCandidateFixture, marcusCandidateFixture], next_cursor: null }}
          initialSelectedCandidate={priyaCandidateFixture}
          initialNearMatches={{ items: [], next_cursor: null }}
          immutableScorecard={null}
        />
      </QueryClientProvider>,
    )

    await client.invalidateQueries({ queryKey: ["job-candidates", priyaCandidateFixture.job_id] })
    await userEvent.click(screen.getByRole("tab", { name: "Near Matches" }))
    expect(await screen.findByText("Avery Stone")).toBeVisible()
  })

  it("opens an authorized deep-linked candidate that is not in the first ranked page", () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    render(
      <QueryClientProvider client={client}>
        <ReviewWorkspace
          jobId={priyaCandidateFixture.job_id}
          initialCandidates={{ items: [priyaCandidateFixture], next_cursor: "next" }}
          initialSelectedCandidate={nearMatchFixture}
          initialNearMatches={{ items: [nearMatchFixture], next_cursor: null }}
          immutableScorecard={null}
        />
      </QueryClientProvider>,
    )
    expect(screen.getByRole("article", { name: "Avery Stone" })).toBeVisible()
  })

  it("shows an empty detail state instead of an endless loader when no main candidate exists", () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <ReviewWorkspace
          jobId={priyaCandidateFixture.job_id}
          initialCandidates={{ items: [], next_cursor: null }}
          initialSelectedCandidate={null}
          initialNearMatches={{ items: [], next_cursor: null }}
          immutableScorecard={null}
        />
      </QueryClientProvider>,
    )
    expect(screen.getByRole("heading", { name: "Select a candidate" })).toBeVisible()
    expect(screen.queryByText("Loading candidate evidence…")).not.toBeInTheDocument()
  })
})
