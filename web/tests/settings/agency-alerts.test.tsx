import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { HttpResponse, http } from "msw"
import { describe, expect, it } from "vitest"

import { AgencyAlerts, formatUtcTimestamp } from "@/components/layout/AgencyAlerts"
import { server } from "@/tests/setup"

const alert = {
  id: "00000000-0000-4000-8000-000000000a01",
  code: "usage_budget_exhausted",
  title: "Sourcing budget reached",
  message: "The authorized sourcing run reached its budget.",
  run_id: "00000000-0000-4000-8000-000000000301",
  acknowledged_at: null,
  created_at: "2026-08-16T00:00:00Z",
}

describe("AgencyAlerts", () => {
  it("formats server-rendered timestamps deterministically in UTC", () => {
    expect(formatUtcTimestamp("2026-08-16T00:00:00Z")).toBe("2026-08-16 00:00 UTC")
  })

  it("acknowledges with one stable intent key and keeps only the run UUID in its activity link", async () => {
    const keys: string[] = []
    server.use(http.patch(`/api/bff/notifications/${alert.id}`, ({ request }) => {
      keys.push(request.headers.get("Idempotency-Key") ?? "")
      if (keys.length === 1) return HttpResponse.json({ code: "temporary" }, { status: 503 })
      return HttpResponse.json({ ...alert, acknowledged_at: "2026-08-16T01:00:00Z" })
    }))
    render(<AgencyAlerts alerts={[alert]} />)

    expect(screen.getByRole("link", { name: "Open Activity" })).toHaveAttribute(
      "href",
      `/jobs/activity?run=${alert.run_id}`,
    )
    await userEvent.click(screen.getByRole("button", { name: "Acknowledge" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("same safe request")
    await userEvent.click(screen.getByRole("button", { name: "Acknowledge" }))
    expect(await screen.findByText("No unread tenant alerts.")).toBeVisible()
    expect(keys).toHaveLength(2)
    expect(keys[0]).toBeTruthy()
    expect(keys[1]).toBe(keys[0])
  })
})
