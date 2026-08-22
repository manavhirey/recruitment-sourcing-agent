import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { HttpResponse, http } from "msw"
import { vi } from "vitest"

import { GenerateScorecardAction } from "@/components/scorecards/GenerateScorecardAction"
import { navigationMocks, server } from "@/tests/setup"

describe("GenerateScorecardAction", () => {
  it("resumes an ungenerated job with one stable mutation intent", async () => {
    const keys: string[] = []
    let attempt = 0
    server.use(
      http.post("/api/bff/jobs/:jobId/scorecard/generate", ({ request }) => {
        keys.push(request.headers.get("Idempotency-Key") ?? "")
        attempt += 1
        if (attempt === 1) {
          return HttpResponse.json({ code: "api_unavailable" }, { status: 503 })
        }
        return HttpResponse.json({ draft_revision: 1 })
      }),
    )
    const generated = vi.fn()
    render(
      <GenerateScorecardAction
        jobId="00000000-0000-4000-8000-000000000101"
        expectedRevision={0}
        onGenerated={generated}
      />,
    )

    await userEvent.click(screen.getByRole("button", { name: "Generate scorecard" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("same safe request")
    await userEvent.click(screen.getByRole("button", { name: "Generate scorecard" }))
    await vi.waitFor(() => expect(generated).toHaveBeenCalledOnce())

    expect(keys).toHaveLength(2)
    expect(new Set(keys).size).toBe(1)
  })

  it("reauthenticates when the open page session has expired", async () => {
    server.use(
      http.post("/api/bff/jobs/:jobId/scorecard/generate", () =>
        HttpResponse.json({ code: "unauthenticated" }, { status: 401 }),
      ),
    )
    render(
      <GenerateScorecardAction
        jobId="00000000-0000-4000-8000-000000000101"
        expectedRevision={0}
      />,
    )

    await userEvent.click(screen.getByRole("button", { name: "Generate scorecard" }))

    await vi.waitFor(() => expect(navigationMocks.replace).toHaveBeenCalled())
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
  })
})
