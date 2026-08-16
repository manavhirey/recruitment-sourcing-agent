import { describe, expect, it, vi } from "vitest"

import { handleTenantSelection } from "@/lib/tenant-action"

const tenantId = "00000000-0000-4000-8000-000000000001"

function selectionRequest(id = tenantId, origin = "https://sourcing.example.com") {
  return new Request("https://sourcing.example.com/api/tenant", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: origin,
      "Sec-Fetch-Site": origin === "https://sourcing.example.com" ? "same-origin" : "cross-site",
    },
    body: JSON.stringify({ tenantId: id }),
  })
}

describe("tenant selection action", () => {
  it("fails closed when no verified or configured option exists", async () => {
    const writeTenant = vi.fn()
    const response = await handleTenantSelection(selectionRequest(), {
      appUrl: "https://sourcing.example.com",
      configuredOptions: undefined,
      readVerifiedOptions: async () => [],
      revalidateMembership: vi.fn(),
      writeTenant,
    })

    expect(response.status).toBe(503)
    expect(writeTenant).not.toHaveBeenCalled()
  })

  it("rejects an ID that was not supplied by verified OIDC or server configuration", async () => {
    const revalidateMembership = vi.fn()
    const response = await handleTenantSelection(
      selectionRequest("00000000-0000-4000-8000-000000000099"),
      {
        appUrl: "https://sourcing.example.com",
        configuredOptions: JSON.stringify([{ id: tenantId, name: "Agency" }]),
        readVerifiedOptions: async () => [],
        revalidateMembership,
        writeTenant: vi.fn(),
      },
    )

    expect(response.status).toBe(404)
    expect(revalidateMembership).not.toHaveBeenCalled()
  })

  it("revalidates backend membership before writing the cookie", async () => {
    const order: string[] = []
    const response = await handleTenantSelection(selectionRequest(), {
      appUrl: "https://sourcing.example.com",
      configuredOptions: undefined,
      readVerifiedOptions: async () => [{ id: tenantId, name: "Agency" }],
      revalidateMembership: async (id) => {
        expect(id).toBe(tenantId)
        order.push("validated")
      },
      writeTenant: async (id) => {
        expect(id).toBe(tenantId)
        order.push("written")
      },
    })

    expect(response.status).toBe(204)
    expect(order).toEqual(["validated", "written"])
  })

  it("rejects cross-origin requests before any membership call", async () => {
    const revalidateMembership = vi.fn()
    const response = await handleTenantSelection(
      selectionRequest(tenantId, "https://evil.example"),
      {
        appUrl: "https://sourcing.example.com",
        configuredOptions: JSON.stringify([{ id: tenantId, name: "Agency" }]),
        readVerifiedOptions: async () => [],
        revalidateMembership,
        writeTenant: vi.fn(),
      },
    )

    expect(response.status).toBe(403)
    expect(revalidateMembership).not.toHaveBeenCalled()
  })

  it("fails safely if the signed cookie cannot be persisted", async () => {
    const response = await handleTenantSelection(selectionRequest(), {
      appUrl: "https://sourcing.example.com",
      configuredOptions: JSON.stringify([{ id: tenantId, name: "Agency" }]),
      readVerifiedOptions: async () => [],
      revalidateMembership: async () => undefined,
      writeTenant: async () => {
        throw new Error("private-cookie-error")
      },
    })

    expect(response.status).toBe(503)
    expect(await response.json()).toEqual({ code: "tenant_selection_unavailable" })
  })

  it("rejects an oversized chunked selection before reading auth state", async () => {
    const readVerifiedOptions = vi.fn()
    const request = new Request("https://sourcing.example.com/api/tenant", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Origin: "https://sourcing.example.com",
        "Sec-Fetch-Site": "same-origin",
      },
      body: new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode("x".repeat(5 * 1024)))
          controller.enqueue(new TextEncoder().encode("x".repeat(5 * 1024)))
          controller.close()
        },
      }),
      duplex: "half",
    } as RequestInit & { duplex: "half" })

    const response = await handleTenantSelection(request, {
      appUrl: "https://sourcing.example.com",
      configuredOptions: undefined,
      readVerifiedOptions,
      revalidateMembership: vi.fn(),
      writeTenant: vi.fn(),
    })

    expect(response.status).toBe(413)
    expect(readVerifiedOptions).not.toHaveBeenCalled()
  })
})
