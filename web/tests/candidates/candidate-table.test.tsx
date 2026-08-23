import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { HttpResponse, http } from "msw"
import { describe, expect, it, vi } from "vitest"

import { CandidateTable } from "@/components/candidates/CandidateTable"
import { priyaCandidateFixture } from "@/tests/review-fixtures"
import { server } from "@/tests/setup"

function renderTable() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CandidateTable
        jobId={priyaCandidateFixture.job_id}
        initialPage={{ items: [priyaCandidateFixture], next_cursor: "stable-cursor" }}
      />
    </QueryClientProvider>,
  )
}

describe("CandidateTable", () => {
  it("uses server-backed safe filters and only offers export for Shortlisted", async () => {
    const requests: URL[] = []
    server.use(http.get(`/api/bff/jobs/${priyaCandidateFixture.job_id}/candidates`, ({ request }) => {
      requests.push(new URL(request.url))
      return HttpResponse.json({ items: [], next_cursor: null })
    }))
    renderTable()

    expect(screen.queryByRole("button", { name: "Export shortlisted CSV" })).not.toBeInTheDocument()
    await userEvent.selectOptions(screen.getByLabelText("Contact availability"), "true")
    await userEvent.selectOptions(screen.getByLabelText("Stage"), "Shortlisted")

    await waitFor(() => expect(requests.at(-1)?.searchParams.get("has_contact")).toBe("true"))
    expect(requests.at(-1)?.searchParams.get("stage")).toBe("Shortlisted")
    expect(window.location.search).toContain("stage=Shortlisted")
    expect(window.location.search).toContain("has_contact=true")
    expect(screen.getByRole("button", { name: "Export shortlisted CSV" })).toBeVisible()
  })

  it("reports bounded fan-out results without claiming atomicity", async () => {
    const second = { ...priyaCandidateFixture, id: "00000000-0000-4000-8000-000000000502", full_name: "Marcus Lee" }
    server.use(
      http.patch(`/api/bff/job-candidates/${priyaCandidateFixture.id}/stage`, () => HttpResponse.json({})),
      http.patch(`/api/bff/job-candidates/${second.id}/stage`, () => HttpResponse.json({ code: "conflict" }, { status: 409 })),
      http.get(`/api/bff/jobs/${priyaCandidateFixture.job_id}/candidates`, () => HttpResponse.json({ items: [priyaCandidateFixture, second], next_cursor: null })),
    )
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <CandidateTable jobId={priyaCandidateFixture.job_id} initialPage={{ items: [priyaCandidateFixture, second], next_cursor: null }} />
      </QueryClientProvider>,
    )

    await userEvent.click(screen.getByLabelText("Select Priya Sharma"))
    await userEvent.click(screen.getByLabelText("Select Marcus Lee"))
    await userEvent.click(screen.getByRole("button", { name: "Apply to 2" }))
    expect(await screen.findByText("Affected 2. Succeeded 1. Failed 1.")).toBeVisible()
  })

  it("clears hidden selections whenever the server-backed view changes", async () => {
    const mutation = vi.fn(() => HttpResponse.json({}))
    server.use(
      http.get(`/api/bff/jobs/${priyaCandidateFixture.job_id}/candidates`, ({ request }) =>
        HttpResponse.json(
          new URL(request.url).searchParams.has("stage")
            ? { items: [], next_cursor: null }
            : { items: [priyaCandidateFixture], next_cursor: "stable-cursor" },
        ),
      ),
      http.patch(`/api/bff/job-candidates/${priyaCandidateFixture.id}/stage`, mutation),
    )
    renderTable()

    await userEvent.click(screen.getByLabelText("Select Priya Sharma"))
    expect(screen.getByRole("button", { name: "Apply to 1" })).toBeEnabled()
    await userEvent.selectOptions(screen.getByLabelText("Stage"), "Reviewed")

    expect(screen.getByRole("button", { name: "Apply to 0" })).toBeDisabled()
    await userEvent.click(screen.getByRole("button", { name: "Apply to 0" }))
    expect(mutation).not.toHaveBeenCalled()
  })

  it("reuses an uncertain export key until the user starts a new export", async () => {
    server.use(http.get(
      `/api/bff/jobs/${priyaCandidateFixture.job_id}/candidates`,
      () => HttpResponse.json({ items: [priyaCandidateFixture], next_cursor: null }),
    ))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <CandidateTable
          jobId={priyaCandidateFixture.job_id}
          initialPage={{ items: [priyaCandidateFixture], next_cursor: null }}
          initialFilters={{ stage: "Shortlisted", hasContact: "", sort: "-score", cursor: null }}
        />
      </QueryClientProvider>,
    )
    const form = screen.getByRole("button", { name: "Export shortlisted CSV" }).closest("form")
    const key = form?.querySelector<HTMLInputElement>('input[name="idempotencyKey"]')
    expect(form).not.toBeNull()
    expect(key).not.toBeNull()

    fireEvent.submit(form!)
    const first = key!.value
    fireEvent.submit(form!)
    expect(key!.value).toBe(first)
    await new Promise((resolve) => setTimeout(resolve, 1_050))
    fireEvent.submit(form!)
    expect(key!.value).toBe(first)
    await new Promise((resolve) => setTimeout(resolve, 1_050))

    const startNew = screen.getByRole("button", { name: "Start new export" })
    expect(startNew).toBeEnabled()
    fireEvent.click(startNew)
    const nextForm = screen.getByRole("button", { name: "Export shortlisted CSV" }).closest("form")
    const nextKey = nextForm?.querySelector<HTMLInputElement>('input[name="idempotencyKey"]')
    fireEvent.submit(nextForm!)
    expect(nextKey!.value).not.toBe(first)
  })
})
