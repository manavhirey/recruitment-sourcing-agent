import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { HttpResponse, http } from "msw"
import { vi } from "vitest"

import { ScorecardEditor } from "@/components/scorecards/ScorecardEditor"
import { requiredInferenceIds } from "@/lib/inference-confirmations"
import {
  manualRequiredDraftFixture,
  scorecardDraftFixture,
} from "@/tests/fixtures"
import { navigationMocks, server } from "@/tests/setup"

describe("ScorecardEditor", () => {
  const allowedIndustryCodes = ["technology.fintech"]

  it("separates extracted criteria from inferred suggestions", () => {
    render(<ScorecardEditor draft={scorecardDraftFixture} allowedIndustryCodes={allowedIndustryCodes} />)

    expect(screen.getByText("From job description")).toBeVisible()
    expect(screen.getByText("Suggested — confirm before use")).toBeVisible()
    expect(
      screen.getByRole("button", { name: "Confirm and source" }),
    ).toBeDisabled()
  })

  it("requires every inference to be confirmed or deleted", async () => {
    render(<ScorecardEditor draft={scorecardDraftFixture} allowedIndustryCodes={allowedIndustryCodes} />)

    const confirmations = screen.getAllByRole("checkbox", {
      name: /Confirm suggested/i,
    })
    for (const checkbox of confirmations) {
      await userEvent.click(checkbox)
    }

    expect(
      screen.getByRole("button", { name: "Confirm and source" }),
    ).toBeEnabled()
  })

  it("restores persisted approvals after a reload", () => {
    const confirmed = requiredInferenceIds(scorecardDraftFixture.draft)
    render(
      <ScorecardEditor
        draft={{
          ...scorecardDraftFixture,
          draft: {
            ...scorecardDraftFixture.draft,
            confirmed_inferred_items: confirmed,
          },
        }}
        allowedIndustryCodes={allowedIndustryCodes}
      />,
    )

    expect(
      screen.getAllByRole("checkbox", { name: /Confirm suggested/i })
        .every((checkbox) => (checkbox as HTMLInputElement).checked),
    ).toBe(true)
    expect(screen.getByRole("button", { name: "Confirm and source" })).toBeEnabled()
  })

  it("invalidates an approval when inferred content changes", async () => {
    const user = userEvent.setup()
    render(<ScorecardEditor draft={scorecardDraftFixture} allowedIndustryCodes={allowedIndustryCodes} />)
    for (const checkbox of screen.getAllByRole("checkbox", { name: /Confirm suggested/i })) {
      await user.click(checkbox)
    }
    const criterion = screen.getByDisplayValue("Led product-led growth")
    await user.clear(criterion)
    await user.type(criterion, "Owned product-led growth")

    expect(
      screen.getByRole("checkbox", { name: /Confirm suggested Owned product-led growth/i }),
    ).not.toBeChecked()
    expect(screen.getByRole("button", { name: "Confirm and source" })).toBeDisabled()
  })

  it("offers only industries assigned to the authorized client", () => {
    render(<ScorecardEditor draft={scorecardDraftFixture} allowedIndustryCodes={allowedIndustryCodes} />)

    expect(screen.getByRole("option", { name: "Fintech" })).toBeInTheDocument()
    expect(screen.queryByRole("option", { name: "Banking" })).not.toBeInTheDocument()
  })

  it("makes extracted role constraints visible, editable, and locally valid", async () => {
    const user = userEvent.setup()
    render(<ScorecardEditor draft={scorecardDraftFixture} allowedIndustryCodes={allowedIndustryCodes} />)

    expect(screen.getByLabelText("Seniority")).toHaveValue("senior")
    expect(screen.getByLabelText("Locations")).toHaveValue("New York, NY")
    expect(screen.getByLabelText("Minimum years")).toHaveValue(5)
    expect(screen.getByLabelText("Maximum years")).toHaveValue(null)
    await user.type(screen.getByLabelText("Maximum years"), "3")

    expect(screen.getByText("Maximum years cannot be less than minimum years.")).toBeVisible()
    expect(screen.getByRole("button", { name: "Confirm and source" })).toBeDisabled()
  })

  it("shows an empty editable draft when extraction requires manual entry", () => {
    render(<ScorecardEditor draft={manualRequiredDraftFixture} allowedIndustryCodes={allowedIndustryCodes} />)

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Automated extraction could not be completed",
    )
    expect(screen.getByLabelText("Target titles")).toHaveValue("")
    expect(screen.queryByText("Payments platform experience")).not.toBeInTheDocument()
  })

  it("does not enable sourcing for an unconfirmed recruiter-entered exclusion", async () => {
    const user = userEvent.setup()
    render(<ScorecardEditor draft={manualRequiredDraftFixture} allowedIndustryCodes={allowedIndustryCodes} />)

    await user.type(screen.getByLabelText("Target titles"), "Product Manager")
    expect(screen.getByLabelText("Target titles")).toHaveValue("Product Manager")
    await user.selectOptions(screen.getByLabelText("Primary industry"), "technology.fintech")
    await user.click(screen.getByRole("button", { name: "Add exclusion" }))

    expect(screen.getByRole("button", { name: "Confirm and source" })).toBeDisabled()
    await user.click(screen.getByRole("checkbox", { name: /lawful, job-related/i }))
    expect(screen.getByRole("button", { name: "Confirm and source" })).toBeEnabled()
  })

  it("uses separate stable keys and resumes safely after confirmation failure", async () => {
    const saveKeys: string[] = []
    const confirmKeys: string[] = []
    const sourceKeys: string[] = []
    const savedConfirmations: string[][] = []
    let confirmAttempt = 0
    server.use(
      http.put("/api/bff/jobs/:jobId/scorecard/draft", async ({ request }) => {
        saveKeys.push(request.headers.get("Idempotency-Key") ?? "")
        const payload = await request.json() as {
          draft: { confirmed_inferred_items: string[] }
        }
        savedConfirmations.push(payload.draft.confirmed_inferred_items)
        return HttpResponse.json({ ...scorecardDraftFixture, draft_revision: 3 })
      }),
      http.post("/api/bff/jobs/:jobId/scorecard/confirm", ({ request }) => {
        confirmKeys.push(request.headers.get("Idempotency-Key") ?? "")
        confirmAttempt += 1
        if (confirmAttempt === 1) return HttpResponse.json({ code: "api_unavailable" }, { status: 503 })
        return HttpResponse.json({ id: "scorecard-1" })
      }),
      http.post("/api/bff/jobs/:jobId/runs", ({ request }) => {
        sourceKeys.push(request.headers.get("Idempotency-Key") ?? "")
        return HttpResponse.json({ id: "run-1" })
      }),
    )
    const started = vi.fn()
    render(<ScorecardEditor draft={scorecardDraftFixture} allowedIndustryCodes={allowedIndustryCodes} onStarted={started} />)
    for (const checkbox of screen.getAllByRole("checkbox", { name: /Confirm suggested/i })) {
      await userEvent.click(checkbox)
    }

    await userEvent.click(screen.getByRole("button", { name: "Confirm and source" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("Retry uses the same safe request")
    await userEvent.click(screen.getByRole("button", { name: "Confirm and source" }))
    await vi.waitFor(() => expect(started).toHaveBeenCalledOnce())

    expect(saveKeys).toHaveLength(1)
    expect(confirmKeys).toHaveLength(2)
    expect(confirmKeys[0]).toBe(confirmKeys[1])
    expect(sourceKeys).toHaveLength(1)
    expect(new Set([saveKeys[0], confirmKeys[0], sourceKeys[0]]).size).toBe(3)
    expect(savedConfirmations).toEqual([
      [...requiredInferenceIds(scorecardDraftFixture.draft)].sort(),
    ])
  })

  it("locks every editor control while a confirmation flow is in flight", async () => {
    const user = userEvent.setup()
    const started = vi.fn()
    let finishSave: ((response: Response) => void) | undefined
    server.use(
      http.put("/api/bff/jobs/:jobId/scorecard/draft", () =>
        new Promise<Response>((resolve) => {
          finishSave = resolve
        }),
      ),
      http.post("/api/bff/jobs/:jobId/scorecard/confirm", () =>
        HttpResponse.json({ id: "scorecard-1" }),
      ),
      http.post("/api/bff/jobs/:jobId/runs", () =>
        HttpResponse.json({ id: "run-1" }),
      ),
    )
    render(
      <ScorecardEditor
        draft={scorecardDraftFixture}
        allowedIndustryCodes={allowedIndustryCodes}
        onStarted={started}
      />,
    )
    for (const checkbox of screen.getAllByRole("checkbox", { name: /Confirm suggested/i })) {
      await user.click(checkbox)
    }

    await user.click(screen.getByRole("button", { name: "Confirm and source" }))
    await vi.waitFor(() => expect(finishSave).toBeDefined())

    expect(screen.getByLabelText("Target titles")).toBeDisabled()
    expect(screen.getByDisplayValue("Led product-led growth")).toBeDisabled()
    expect(screen.getAllByRole("checkbox", { name: /Confirm suggested/i })[0]).toBeDisabled()
    finishSave?.(HttpResponse.json({ ...scorecardDraftFixture, draft_revision: 3 }))
    await vi.waitFor(() => expect(started).toHaveBeenCalledOnce())
  })

  it("attaches an authoritative client-industry failure to the selector", async () => {
    const user = userEvent.setup()
    server.use(
      http.put("/api/bff/jobs/:jobId/scorecard/draft", () =>
        HttpResponse.json({ ...scorecardDraftFixture, draft_revision: 3 }),
      ),
      http.post("/api/bff/jobs/:jobId/scorecard/confirm", () =>
        HttpResponse.json({ code: "scorecard_industry_invalid" }, { status: 400 }),
      ),
    )
    render(<ScorecardEditor draft={scorecardDraftFixture} allowedIndustryCodes={allowedIndustryCodes} />)
    for (const checkbox of screen.getAllByRole("checkbox", { name: /Confirm suggested/i })) {
      await user.click(checkbox)
    }

    await user.click(screen.getByRole("button", { name: "Confirm and source" }))

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Choose an industry assigned to this client",
    )
    expect(screen.getByLabelText("Primary industry")).toHaveAttribute("aria-invalid", "true")
  })

  it("resumes a persisted confirmed version without creating another version", async () => {
    const user = userEvent.setup()
    const sourceKeys: string[] = []
    const saved = vi.fn()
    const confirmed = vi.fn()
    let attempt = 0
    server.use(
      http.put("/api/bff/jobs/:jobId/scorecard/draft", saved),
      http.post("/api/bff/jobs/:jobId/scorecard/confirm", confirmed),
      http.post("/api/bff/jobs/:jobId/runs", ({ request }) => {
        sourceKeys.push(request.headers.get("Idempotency-Key") ?? "")
        attempt += 1
        if (attempt === 1) {
          return HttpResponse.json({ code: "api_unavailable" }, { status: 503 })
        }
        return HttpResponse.json({ id: "run-1" })
      }),
    )
    const started = vi.fn()
    render(
      <ScorecardEditor
        draft={scorecardDraftFixture}
        allowedIndustryCodes={allowedIndustryCodes}
        alreadyConfirmed
        onStarted={started}
      />,
    )

    expect(screen.queryByLabelText("Target titles")).not.toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "Start sourcing" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("same safe request")
    await user.click(screen.getByRole("button", { name: "Start sourcing" }))
    await vi.waitFor(() => expect(started).toHaveBeenCalledOnce())

    expect(saved).not.toHaveBeenCalled()
    expect(confirmed).not.toHaveBeenCalled()
    expect(sourceKeys).toHaveLength(2)
    expect(new Set(sourceKeys).size).toBe(1)
  })

  it("reauthenticates when sourcing from an open page with an expired session", async () => {
    server.use(
      http.post("/api/bff/jobs/:jobId/runs", () =>
        HttpResponse.json({ code: "unauthenticated" }, { status: 401 }),
      ),
    )
    render(
      <ScorecardEditor
        draft={scorecardDraftFixture}
        allowedIndustryCodes={allowedIndustryCodes}
        alreadyConfirmed
      />,
    )

    await userEvent.click(screen.getByRole("button", { name: "Start sourcing" }))

    await vi.waitFor(() => expect(navigationMocks.replace).toHaveBeenCalled())
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
  })
})
