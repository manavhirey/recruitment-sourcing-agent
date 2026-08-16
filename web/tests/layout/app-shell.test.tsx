import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { HttpResponse, http } from "msw"
import { vi } from "vitest"

import { AppShell } from "@/components/layout/AppShell"
import { navigationMocks, server } from "@/tests/setup"

describe("AppShell", () => {
  it("renders semantic agency navigation and active jobs without exposing server tokens", () => {
    const { container } = render(
      <AppShell
        agency={{
          id: "00000000-0000-4000-8000-000000000001",
          name: "Northstar Search",
        }}
        user={{
          name: "Avery Stone",
          email: "avery@northstar.test",
          accessToken: "must-never-render",
        } as never}
        role="owner"
        tenantOptions={[
          {
            id: "00000000-0000-4000-8000-000000000001",
            name: "Northstar Search",
          },
        ]}
        activeJobs={[
          {
            id: "00000000-0000-4000-8000-000000000301",
            title: "Senior Product Manager",
            status: "awaiting_scorecard",
          },
        ]}
      >
        <h1>Jobs</h1>
      </AppShell>,
    )

    expect(screen.getByRole("navigation", { name: "Primary" })).toBeVisible()
    for (const route of ["Jobs", "Candidates", "Clients", "Settings"]) {
      expect(screen.getByRole("link", { name: route })).toBeVisible()
    }
    expect(screen.getByRole("complementary", { name: "Active jobs" })).toHaveTextContent(
      "Senior Product Manager",
    )
    expect(screen.getByText("Northstar Search")).toBeVisible()
    expect(container.innerHTML).not.toContain("must-never-render")
    expect(container.querySelector("main h1")).toHaveTextContent("Jobs")
  })

  it("keeps the current agency selected when switching is unreachable", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("network unavailable"))
    render(
      <AppShell
        agency={{ id: "00000000-0000-4000-8000-000000000001", name: "Northstar Search" }}
        user={{ name: "Avery Stone" }}
        role="owner"
        tenantOptions={[
          { id: "00000000-0000-4000-8000-000000000001", name: "Northstar Search" },
          { id: "00000000-0000-4000-8000-000000000002", name: "Second Agency" },
        ]}
        activeJobs={[]}
      >
        <h1>Jobs</h1>
      </AppShell>,
    )

    await userEvent.selectOptions(screen.getByLabelText("Agency"), "00000000-0000-4000-8000-000000000002")

    await vi.waitFor(() => expect(screen.getByLabelText("Agency")).toBeEnabled())
    expect(screen.getByLabelText("Agency")).toHaveValue("00000000-0000-4000-8000-000000000001")
  })

  it("reauthenticates when agency switching reports an expired session", async () => {
    server.use(
      http.post("/api/tenant", () =>
        HttpResponse.json({ code: "unauthenticated" }, { status: 401 }),
      ),
    )
    render(
      <AppShell
        agency={{ id: "00000000-0000-4000-8000-000000000001", name: "Northstar Search" }}
        user={{ name: "Avery Stone" }}
        role="owner"
        tenantOptions={[
          { id: "00000000-0000-4000-8000-000000000001", name: "Northstar Search" },
          { id: "00000000-0000-4000-8000-000000000002", name: "Second Agency" },
        ]}
        activeJobs={[]}
      >
        <h1>Jobs</h1>
      </AppShell>,
    )

    await userEvent.selectOptions(
      screen.getByLabelText("Agency"),
      "00000000-0000-4000-8000-000000000002",
    )

    await vi.waitFor(() => expect(navigationMocks.replace).toHaveBeenCalled())
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
  })
})
