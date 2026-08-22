import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { HttpResponse, http } from "msw"
import { vi } from "vitest"

import { TenantChooser } from "@/components/layout/TenantChooser"
import { navigationMocks, server } from "@/tests/setup"

describe("TenantChooser", () => {
  it("announces a safe error when tenant verification is unreachable", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("network unavailable"))
    render(
      <TenantChooser
        options={[
          { id: "00000000-0000-4000-8000-000000000001", name: "Northstar Search" },
        ]}
      />,
    )

    await userEvent.click(screen.getByRole("button", { name: "Continue" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("could not be verified")
    expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled()
  })

  it("reauthenticates when tenant verification reports an expired session", async () => {
    server.use(
      http.post("/api/tenant", () =>
        HttpResponse.json({ code: "unauthenticated" }, { status: 401 }),
      ),
    )
    render(
      <TenantChooser options={[
        { id: "00000000-0000-4000-8000-000000000001", name: "Northstar Search" },
      ]} />,
    )

    await userEvent.click(screen.getByRole("button", { name: "Continue" }))

    await vi.waitFor(() => expect(navigationMocks.replace).toHaveBeenCalled())
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
  })
})
