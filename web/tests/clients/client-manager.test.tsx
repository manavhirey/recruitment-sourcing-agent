import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { http, HttpResponse } from "msw"

import { ClientManager } from "@/components/clients/ClientManager"
import { authorizedClientsFixture } from "@/tests/fixtures"
import { navigationMocks, server } from "@/tests/setup"

const recruiter = {
  membership_id: "00000000-0000-4000-8000-000000000301",
  user_id: "00000000-0000-4000-8000-000000000302",
  email: "recruiter@example.test",
  display_name: "Morgan Recruiter",
  role: "recruiter" as const,
  allowed_client_ids: [],
  active: true,
}

describe("ClientManager", () => {
  it("programmatically attaches and announces client creation errors", async () => {
    render(<ClientManager clients={[]} role="owner" />)
    await userEvent.click(screen.getByText("Add client"))
    await userEvent.click(screen.getByRole("button", { name: "Create client" }))

    const name = screen.getByLabelText("Client name")
    const industry = screen.getByLabelText("Primary industry")
    expect(name).toHaveAttribute("aria-describedby", "client-name-error")
    expect(industry).toHaveAttribute("aria-describedby", "client-industry-error")
    expect(document.getElementById("client-name-error")).toHaveAttribute("role", "alert")
    expect(document.getElementById("client-industry-error")).toHaveAttribute("role", "alert")
  })

  it("hides management controls from recruiters", () => {
    render(
      <ClientManager clients={authorizedClientsFixture} role="recruiter" />,
    )

    expect(screen.getByText("PayFlow")).toBeVisible()
    expect(
      screen.queryByRole("button", { name: "Add client" }),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Grant recruiter access" }),
    ).not.toBeInTheDocument()
  })

  it("uses the controlled taxonomy and a stable key when an industry update is retried", async () => {
    const user = userEvent.setup()
    const idempotencyKeys: string[] = []
    let attempts = 0
    server.use(
      http.put("/api/bff/clients/:clientId/industries", async ({ request }) => {
        idempotencyKeys.push(request.headers.get("Idempotency-Key") ?? "")
        attempts += 1
        if (attempts === 1) return HttpResponse.json({ code: "api_unavailable" }, { status: 503 })
        return HttpResponse.json({
          ...authorizedClientsFixture[0],
          industry_codes: ["healthcare"],
        })
      }),
    )
    render(
      <ClientManager clients={authorizedClientsFixture} members={[recruiter]} role="admin" />,
    )

    await user.click(screen.getByText("Manage PayFlow"))
    await user.selectOptions(screen.getByLabelText("Primary industries for PayFlow"), "healthcare")
    await user.click(screen.getByRole("button", { name: "Update industries" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("could not be updated")
    await user.click(screen.getByRole("button", { name: "Update industries" }))

    await waitFor(() => expect(attempts).toBe(2))
    expect(idempotencyKeys[0]).toBeTruthy()
    expect(idempotencyKeys[1]).toBe(idempotencyKeys[0])
    expect(await screen.findByText("Industries updated.")).toBeVisible()
  })

  it("submits only an active recruiter membership for a client grant", async () => {
    const user = userEvent.setup()
    let requestBody: unknown
    server.use(
      http.post("/api/bff/clients/:clientId/grants", async ({ request }) => {
        requestBody = await request.json()
        return HttpResponse.json({
          client_id: authorizedClientsFixture[0].id,
          membership_id: recruiter.membership_id,
        })
      }),
    )
    render(
      <ClientManager
        clients={authorizedClientsFixture}
        members={[
          recruiter,
          { ...recruiter, membership_id: "00000000-0000-4000-8000-000000000303", active: false },
        ]}
        role="owner"
      />,
    )

    await user.click(screen.getByText("Manage PayFlow"))
    await user.selectOptions(screen.getByLabelText("Recruiter access for PayFlow"), recruiter.membership_id)
    await user.click(screen.getByRole("button", { name: "Grant recruiter access" }))

    await waitFor(() => expect(requestBody).toEqual({ membership_id: recruiter.membership_id }))
    expect(await screen.findByText("Recruiter access granted.")).toBeVisible()
  })

  it("announces a safe error when the client service is unreachable", async () => {
    const user = userEvent.setup()
    server.use(
      http.put("/api/bff/clients/:clientId/industries", () => HttpResponse.error()),
    )
    render(<ClientManager clients={authorizedClientsFixture} role="admin" />)

    await user.click(screen.getByText("Manage PayFlow"))
    await user.click(screen.getByRole("button", { name: "Update industries" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("could not be updated")
  })

  it("announces a safe error when client creation is unreachable", async () => {
    const user = userEvent.setup()
    server.use(http.post("/api/bff/clients", () => HttpResponse.error()))
    render(<ClientManager clients={[]} role="owner" />)

    await user.click(screen.getByText("Add client"))
    await user.type(screen.getByLabelText("Client name"), "PayFlow")
    await user.selectOptions(screen.getByLabelText("Primary industry"), "technology.fintech")
    await user.click(screen.getByRole("button", { name: "Create client" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("could not be added")
  })

  it("reauthenticates instead of retrying a client mutation after expiry", async () => {
    server.use(
      http.put("/api/bff/clients/:clientId/industries", () =>
        HttpResponse.json({ code: "unauthenticated" }, { status: 401 }),
      ),
    )
    render(<ClientManager clients={authorizedClientsFixture} role="admin" />)

    await userEvent.click(screen.getByText("Manage PayFlow"))
    await userEvent.click(screen.getByRole("button", { name: "Update industries" }))

    await waitFor(() => expect(navigationMocks.replace).toHaveBeenCalled())
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
  })
})
