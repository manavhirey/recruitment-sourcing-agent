import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { HttpResponse, http } from "msw"
import { describe, expect, it, vi } from "vitest"

import { RunStatus, runPollingInterval } from "@/components/jobs/RunStatus"
import { server } from "@/tests/setup"

const run = {
  id: "00000000-0000-4000-8000-000000000301",
  tenant_id: "00000000-0000-4000-8000-000000000001",
  job_id: "00000000-0000-4000-8000-000000000101",
  scorecard_version_id: "00000000-0000-4000-8000-000000000401",
  state: "matching" as const,
  current_stage: "matching",
  candidate_count: 173,
  matched_count: 42,
  enriched_count: 12,
  failed_count: 2,
  cancellation_requested: false,
  budget_use: { search_pages: 4, enrichments: 12, estimated_credits: 31 },
  error_code: null,
  error_message: null,
  created_at: "2026-08-16T12:00:00Z",
  started_at: "2026-08-16T12:00:01Z",
  completed_at: null,
  updated_at: "2026-08-16T12:01:00Z",
}

describe("RunStatus", () => {
  it("uses progressive polling intervals and stops at terminal states", () => {
    expect(runPollingInterval("matching")).toBe(3_000)
    expect(runPollingInterval("partially_ready")).toBe(10_000)
    expect(runPollingInterval("ready")).toBe(false)
    expect(runPollingInterval("cancelled")).toBe(false)
    expect(runPollingInterval("failed")).toBe(false)
  })

  it("shows only the allowlisted status counters and sanitized stage error", async () => {
    server.use(
      http.get(`/api/bff/runs/${run.id}`, () => HttpResponse.json(run)),
    )
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <RunStatus jobId={run.job_id} initialRun={{ ...run, error_code: "provider_secret", error_message: "token=secret" }} />
      </QueryClientProvider>,
    )

    expect(screen.getByText("173")).toBeVisible()
    expect(screen.getByText("42")).toBeVisible()
    expect(screen.getByText("12")).toBeVisible()
    expect(screen.getByText("2")).toBeVisible()
    expect(screen.queryByText(/token=secret/)).not.toBeInTheDocument()
  })

  it("requires confirmation before cancellation", async () => {
    const post = vi.fn(() => HttpResponse.json({ ...run, state: "cancelled" }))
    server.use(http.post(`/api/bff/runs/${run.id}/cancel`, post))
    server.use(http.get(`/api/bff/runs/${run.id}`, () => HttpResponse.json(run)))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <RunStatus jobId={run.job_id} initialRun={run} />
      </QueryClientProvider>,
    )

    await userEvent.click(screen.getByRole("button", { name: "Cancel sourcing" }))
    expect(screen.getByRole("dialog", { name: "Cancel sourcing run?" })).toBeVisible()
    expect(post).not.toHaveBeenCalled()
  })

  it("traps dialog focus, closes on Escape, and restores the opener", async () => {
    server.use(http.get(`/api/bff/runs/${run.id}`, () => HttpResponse.json(run)))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><RunStatus jobId={run.job_id} initialRun={run} /></QueryClientProvider>)
    const opener = screen.getByRole("button", { name: "Cancel sourcing" })

    await userEvent.click(opener)
    const confirm = screen.getByRole("button", { name: "Confirm cancellation" })
    expect(confirm).toHaveFocus()
    await userEvent.tab()
    expect(screen.getByRole("button", { name: "Keep running" })).toHaveFocus()
    await userEvent.keyboard("{Escape}")
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
    expect(opener).toHaveFocus()
  })

  it("reuses the cancellation key after an authoritative failure and explicit retry", async () => {
    const keys: string[] = []
    server.use(
      http.get(`/api/bff/runs/${run.id}`, () => HttpResponse.json(run)),
      http.post(`/api/bff/runs/${run.id}/cancel`, ({ request }) => {
        keys.push(request.headers.get("Idempotency-Key") ?? "")
        return keys.length === 1
          ? HttpResponse.json({ code: "conflict" }, { status: 409 })
          : HttpResponse.json({ ...run, state: "cancelled" })
      }),
    )
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(<QueryClientProvider client={client}><RunStatus jobId={run.job_id} initialRun={run} /></QueryClientProvider>)

    await userEvent.click(screen.getByRole("button", { name: "Cancel sourcing" }))
    await userEvent.click(screen.getByRole("button", { name: "Confirm cancellation" }))
    expect(await screen.findByText(/Cancellation was not confirmed/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Cancel sourcing" }))
    await userEvent.click(screen.getByRole("button", { name: "Confirm cancellation" }))

    expect(keys).toHaveLength(2)
    expect(keys[0]).toBeTruthy()
    expect(keys[1]).toBe(keys[0])
  })

  it("cannot reopen cancellation and clear its intent key while the request is in flight", async () => {
    const keys: string[] = []
    let releaseFailure: (() => void) | undefined
    const firstFailure = new Promise<void>((resolve) => { releaseFailure = resolve })
    server.use(
      http.get(`/api/bff/runs/${run.id}`, () => HttpResponse.json(run)),
      http.post(`/api/bff/runs/${run.id}/cancel`, async ({ request }) => {
        keys.push(request.headers.get("Idempotency-Key") ?? "")
        if (keys.length === 1) {
          await firstFailure
          return HttpResponse.json({ code: "conflict" }, { status: 409 })
        }
        return HttpResponse.json({ ...run, state: "cancelled" })
      }),
    )
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(<QueryClientProvider client={client}><RunStatus jobId={run.job_id} initialRun={run} /></QueryClientProvider>)

    await userEvent.click(screen.getByRole("button", { name: "Cancel sourcing" }))
    await userEvent.click(screen.getByRole("button", { name: "Confirm cancellation" }))
    expect(screen.queryByRole("button", { name: "Cancel sourcing" })).not.toBeInTheDocument()
    releaseFailure?.()
    expect(await screen.findByText(/Cancellation was not confirmed/)).toBeVisible()
    await userEvent.click(screen.getByRole("button", { name: "Cancel sourcing" }))
    await userEvent.click(screen.getByRole("button", { name: "Confirm cancellation" }))

    expect(keys).toHaveLength(2)
    expect(keys[1]).toBe(keys[0])
  })
})
