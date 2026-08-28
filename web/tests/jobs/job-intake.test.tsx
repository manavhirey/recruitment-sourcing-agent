import { fireEvent, render, screen } from "@testing-library/react"
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

  it("extracts a PDF into the editable job-description field without including source metadata in job creation", async () => {
    let createBody: Record<string, unknown> | undefined
    server.use(
      http.post("/api/bff/job-descriptions/extract", () =>
        HttpResponse.json({
          text: "Senior Product Designer\nLead growth design.",
          source: { filename: "role.pdf", media_type: "application/pdf" },
        }),
      ),
      http.post("/api/bff/jobs", async ({ request }) => {
        createBody = await request.json() as Record<string, unknown>
        return HttpResponse.json({
          id: "00000000-0000-4000-8000-000000000301",
          tenant_id: "00000000-0000-4000-8000-000000000001",
          client_id: authorizedClientsFixture[0].id,
          owner_user_id: "00000000-0000-4000-8000-000000000401",
          title: "Product Designer",
          job_description: "Senior Product Designer\nLead growth design. Edited",
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
          original_job_description: "Senior Product Designer\nLead growth design. Edited",
          extraction_status: "manual_required",
          extraction_warning: "Enter manually",
          seniority_options: [],
        }),
      ),
    )
    const ready = vi.fn()
    const user = userEvent.setup()
    render(<JobIntakeForm clients={authorizedClientsFixture} onDraftReady={ready} />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" }),
    )
    const description = await screen.findByLabelText("Job description")
    expect(description).toHaveValue("Senior Product Designer\nLead growth design.")
    await user.type(description, " Edited")
    await user.selectOptions(screen.getByLabelText("Client"), authorizedClientsFixture[0].id)
    await user.type(screen.getByLabelText("Job title"), "Product Designer")
    await user.click(screen.getByRole("button", { name: "Generate scorecard" }))

    await vi.waitFor(() => expect(ready).toHaveBeenCalledOnce())
    expect(createBody).toEqual({
      client_id: authorizedClientsFixture[0].id,
      title: "Product Designer",
      job_description: "Senior Product Designer\nLead growth design. Edited",
      location: null,
      employment_model: null,
    })
  })

  it("disables generation while an extraction is in progress while preserving paste-only intake", async () => {
    let resolveExtraction: ((value: Response) => void) | undefined
    server.use(http.post("/api/bff/job-descriptions/extract", () => new Promise<Response>((resolve) => {
      resolveExtraction = resolve
    })))
    const user = userEvent.setup()
    render(<JobIntakeForm clients={authorizedClientsFixture} />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" }),
    )
    expect(await screen.findByRole("status")).toHaveTextContent("Extracting job description…")
    expect(screen.getByRole("button", { name: "Generate scorecard" })).toBeDisabled()
    resolveExtraction?.(HttpResponse.json({
      text: "Pasted-or-extracted job description",
      source: { filename: "role.pdf", media_type: "application/pdf" },
    }))
    expect(await screen.findByText("Extracted from role.pdf")).toBeVisible()
  })

  it("locks job-description editing until the deferred extraction response is applied", async () => {
    let resolveExtraction: ((value: Response) => void) | undefined
    server.use(http.post("/api/bff/job-descriptions/extract", () => new Promise<Response>((resolve) => {
      resolveExtraction = resolve
    })))
    const user = userEvent.setup()
    render(<JobIntakeForm clients={authorizedClientsFixture} />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" }),
    )
    const description = screen.getByLabelText("Job description")
    expect(await screen.findByRole("status")).toHaveTextContent("Extracting job description…")
    expect(description).toHaveAttribute("readonly")
    await user.type(description, "Late text")
    expect(description).toHaveValue("")

    resolveExtraction?.(HttpResponse.json({
      text: "Extracted text",
      source: { filename: "role.pdf", media_type: "application/pdf" },
    }))
    expect(await screen.findByText("Extracted from role.pdf")).toBeVisible()
    expect(description).not.toHaveAttribute("readonly")
    await user.type(description, " Edited")
    expect(description).toHaveValue("Extracted text Edited")
  })

  it("does not start an extraction retry while job submission is active", async () => {
    let extractionRequests = 0
    let resolveJob: ((value: Response) => void) | undefined
    server.use(
      http.post("/api/bff/job-descriptions/extract", () => {
        extractionRequests += 1
        return HttpResponse.json(
          { code: "job_description_extraction_unavailable" },
          { status: 503 },
        )
      }),
      http.post("/api/bff/jobs", () => new Promise<Response>((resolve) => {
        resolveJob = resolve
      })),
      http.post("/api/bff/jobs/:jobId/scorecard/generate", () =>
        HttpResponse.json({
          job_id: "00000000-0000-4000-8000-000000000301",
          draft_revision: 1,
          draft: { target_titles: [], criteria: [], seniority: [], minimum_years: null, maximum_years: null, locations: [], industry_code: "", suggested_adjacent_industries: [], uncertainties: [] },
          original_job_description: "Pasted role",
          extraction_status: "manual_required",
          extraction_warning: "Enter manually",
          seniority_options: [],
        }),
      ),
    )
    const ready = vi.fn()
    const user = userEvent.setup()
    render(<JobIntakeForm clients={authorizedClientsFixture} onDraftReady={ready} />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" }),
    )
    const retry = await screen.findByRole("button", { name: "Try again" })
    await user.selectOptions(screen.getByLabelText("Client"), authorizedClientsFixture[0].id)
    await user.type(screen.getByLabelText("Job title"), "Product Designer")
    await user.type(screen.getByLabelText("Job description"), "Pasted role")
    await user.click(screen.getByRole("button", { name: "Generate scorecard" }))

    expect(await screen.findByRole("button", { name: "Generating…" })).toBeDisabled()
    expect(retry).toBeDisabled()
    fireEvent.click(retry)
    expect(extractionRequests).toBe(1)

    resolveJob?.(HttpResponse.json({
      id: "00000000-0000-4000-8000-000000000301",
      tenant_id: "00000000-0000-4000-8000-000000000001",
      client_id: authorizedClientsFixture[0].id,
      owner_user_id: "00000000-0000-4000-8000-000000000401",
      title: "Product Designer",
      job_description: "Pasted role",
      location: null,
      employment_model: null,
      status: "awaiting_scorecard",
      draft_revision: 0,
      extraction_status: "ready",
      extraction_warning: null,
      current_scorecard_id: null,
      created_at: "2026-08-16T00:00:00Z",
      updated_at: "2026-08-16T00:00:00Z",
    }))
    await vi.waitFor(() => expect(ready).toHaveBeenCalledOnce())
  })

  it("reauthenticates an expired extraction session with the current callback URL", async () => {
    window.history.replaceState({}, "", "/jobs?client=authorized")
    server.use(
      http.post("/api/bff/job-descriptions/extract", () =>
        HttpResponse.json({ code: "unauthenticated" }, { status: 401 }),
      ),
    )
    const user = userEvent.setup()
    render(<JobIntakeForm clients={authorizedClientsFixture} />)

    await user.upload(
      screen.getByLabelText("Upload job description"),
      new File(["%PDF-1.4"], "role.pdf", { type: "application/pdf" }),
    )

    await vi.waitFor(() => expect(navigationMocks.replace).toHaveBeenCalledWith(
      "/api/auth/signin?callbackUrl=%2Fjobs%3Fclient%3Dauthorized",
    ))
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument()
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
          seniority_options: [
            { value: "early_career", label: "Early-Career", minimum_years: 0, maximum_years: 3 },
            { value: "mid_level", label: "Mid-Level", minimum_years: 3, maximum_years: 9 },
            { value: "senior", label: "Senior", minimum_years: 10, maximum_years: null },
          ],
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
