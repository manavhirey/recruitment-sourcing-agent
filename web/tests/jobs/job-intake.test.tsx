import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { HttpResponse, http } from "msw"
import { vi } from "vitest"

import { JobIntakeForm } from "@/components/jobs/JobIntakeForm"
import { authorizedClientsFixture } from "@/tests/fixtures"
import { navigationMocks, server } from "@/tests/setup"

describe("JobIntakeForm", () => {
  it("requires a client and job description", async () => {
    render(<JobIntakeForm clients={authorizedClientsFixture} />)

    await userEvent.click(
      screen.getByRole("button", { name: "Generate scorecard" }),
    )

    expect(await screen.findByText("Select a client")).toBeVisible()
    expect(screen.getByText("Enter a job description")).toBeVisible()
    expect(screen.getByLabelText("Client")).toHaveAttribute(
      "aria-describedby",
      "client-id-error",
    )
    expect(screen.getByLabelText("Job description")).toHaveAttribute(
      "aria-describedby",
      "job-description-hint job-description-error",
    )
    expect(screen.getByText("Select a client")).toHaveAttribute("role", "alert")
  })

  it("reuses the same creation idempotency key when a safe retry is needed", async () => {
    const keys: string[] = []
    let attempt = 0
    server.use(
      http.post("/api/bff/jobs", ({ request }) => {
        keys.push(request.headers.get("Idempotency-Key") ?? "")
        attempt += 1
        if (attempt === 1) return HttpResponse.json({ code: "api_unavailable" }, { status: 503 })
        return HttpResponse.json({
          id: "00000000-0000-4000-8000-000000000301",
          tenant_id: "00000000-0000-4000-8000-000000000001",
          client_id: authorizedClientsFixture[0].id,
          owner_user_id: "00000000-0000-4000-8000-000000000401",
          title: "Product Manager",
          job_description: "Payments role",
          location: null,
          employment_model: null,
          status: "awaiting_scorecard",
          draft_revision: 0,
          extraction_status: "ready",
          extraction_warning: null,
          current_scorecard_id: null,
          created_at: "2026-08-16T00:00:00Z",
          updated_at: "2026-08-16T00:00:00Z",
        })
      }),
      http.post("/api/bff/jobs/:jobId/scorecard/generate", () =>
        HttpResponse.json({
          job_id: "00000000-0000-4000-8000-000000000301",
          draft_revision: 1,
          draft: { target_titles: [], criteria: [], seniority: [], minimum_years: null, maximum_years: null, locations: [], industry_code: "", suggested_adjacent_industries: [], uncertainties: [] },
          original_job_description: "Payments role",
          extraction_status: "manual_required",
          extraction_warning: "Enter manually",
        }),
      ),
    )
    const ready = vi.fn()
    render(<JobIntakeForm clients={authorizedClientsFixture} onDraftReady={ready} />)
    await userEvent.selectOptions(screen.getByLabelText("Client"), authorizedClientsFixture[0].id)
    await userEvent.type(screen.getByLabelText("Job title"), "Product Manager")
    await userEvent.type(screen.getByLabelText("Job description"), "Payments role")

    await userEvent.click(screen.getByRole("button", { name: "Generate scorecard" }))
    expect(await screen.findByRole("alert")).toBeVisible()
    await userEvent.click(screen.getByRole("button", { name: "Generate scorecard" }))

    await vi.waitFor(() => expect(ready).toHaveBeenCalledOnce())
    expect(keys).toHaveLength(2)
    expect(keys[0]).toBe(keys[1])
  })

  it("attaches a stale client authorization error to the client field", async () => {
    server.use(
      http.post("/api/bff/jobs", () =>
        HttpResponse.json({ code: "job_not_found" }, { status: 404 }),
      ),
    )
    render(<JobIntakeForm clients={authorizedClientsFixture} />)
    await userEvent.selectOptions(screen.getByLabelText("Client"), authorizedClientsFixture[0].id)
    await userEvent.type(screen.getByLabelText("Job title"), "Product Manager")
    await userEvent.type(screen.getByLabelText("Job description"), "Payments role")

    await userEvent.click(screen.getByRole("button", { name: "Generate scorecard" }))

    expect(await screen.findByText("This client is no longer available")).toBeVisible()
    expect(screen.getByLabelText("Client")).toHaveAttribute("aria-invalid", "true")
  })

  it("reauthenticates instead of offering a futile retry after session expiry", async () => {
    server.use(
      http.post("/api/bff/jobs", () =>
        HttpResponse.json({ code: "unauthenticated" }, { status: 401 }),
      ),
    )
    render(<JobIntakeForm clients={authorizedClientsFixture} />)
    await userEvent.selectOptions(screen.getByLabelText("Client"), authorizedClientsFixture[0].id)
    await userEvent.type(screen.getByLabelText("Job title"), "Product Manager")
    await userEvent.type(screen.getByLabelText("Job description"), "Payments role")

    await userEvent.click(screen.getByRole("button", { name: "Generate scorecard" }))

    await vi.waitFor(() => expect(navigationMocks.replace).toHaveBeenCalledWith(
      "/api/auth/signin?callbackUrl=%2Fjobs",
    ))
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
  })
})
