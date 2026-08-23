import { z } from "zod"
import { describe, expect, it, vi } from "vitest"

import { handleBffMutation, handleBffRead, handleBffStream } from "@/lib/bff"

const tenantId = "00000000-0000-4000-8000-000000000001"

function request(origin = "https://sourcing.example.com", idempotencyKey = "intent-key") {
  return new Request("https://sourcing.example.com/api/bff/clients", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
      Origin: origin,
      "Sec-Fetch-Site": origin === "https://sourcing.example.com" ? "same-origin" : "cross-site",
    },
    body: JSON.stringify({ name: "PayFlow" }),
  })
}

describe("BFF mutation boundary", () => {
  it("rejects a missing or tampered selected-tenant cookie before the backend call", async () => {
    const callApi = vi.fn()
    const response = await handleBffMutation(request(), {
      appUrl: "https://sourcing.example.com",
      path: "/api/v1/clients",
      method: "POST",
      schema: z.object({ name: z.string() }),
      readTenant: async () => null,
      callApi,
    })

    expect(response.status).toBe(401)
    expect(callApi).not.toHaveBeenCalled()
  })

  it("forwards only validated data with the caller's stable idempotency key", async () => {
    const callApi = vi.fn().mockResolvedValue({ id: "client-1" })
    const response = await handleBffMutation(request(), {
      appUrl: "https://sourcing.example.com",
      path: "/api/v1/clients",
      method: "POST",
      schema: z.object({ name: z.string() }).strip(),
      readTenant: async () => tenantId,
      callApi,
    })

    expect(response.status).toBe(200)
    expect(response.headers.get("Cache-Control")).toBe("private, no-store")
    expect(callApi).toHaveBeenCalledWith(
      "/api/v1/clients",
      tenantId,
      expect.objectContaining({
        method: "POST",
        idempotencyKey: "intent-key",
        body: JSON.stringify({ name: "PayFlow" }),
      }),
    )
  })

  it("does not forward cross-origin or missing-idempotency requests", async () => {
    const callApi = vi.fn()
    const crossOrigin = await handleBffMutation(request("https://evil.example"), {
      appUrl: "https://sourcing.example.com",
      path: "/api/v1/clients",
      method: "POST",
      schema: z.object({ name: z.string() }),
      readTenant: async () => tenantId,
      callApi,
    })
    const missingKey = await handleBffMutation(request("https://sourcing.example.com", ""), {
      appUrl: "https://sourcing.example.com",
      path: "/api/v1/clients",
      method: "POST",
      schema: z.object({ name: z.string() }),
      readTenant: async () => tenantId,
      callApi,
    })

    expect(crossOrigin.status).toBe(403)
    expect(missingKey.status).toBe(400)
    expect(callApi).not.toHaveBeenCalled()
  })

  it("returns a safe error when the signed tenant cookie cannot be read", async () => {
    const callApi = vi.fn()
    const response = await handleBffMutation(request(), {
      appUrl: "https://sourcing.example.com",
      path: "/api/v1/clients",
      method: "POST",
      schema: z.object({ name: z.string() }),
      readTenant: async () => {
        throw new Error("private-cookie-error")
      },
      callApi,
    })

    expect(response.status).toBe(503)
    expect(await response.json()).toEqual({ code: "tenant_unavailable" })
    expect(callApi).not.toHaveBeenCalled()
  })

  it("stops reading an oversized chunked body before backend work", async () => {
    const callApi = vi.fn()
    const oversized = new Request("https://sourcing.example.com/api/bff/clients", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": "bounded-intent",
        Origin: "https://sourcing.example.com",
        "Sec-Fetch-Site": "same-origin",
      },
      body: new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode("x".repeat(80 * 1024)))
          controller.enqueue(new TextEncoder().encode("x".repeat(80 * 1024)))
          controller.close()
        },
      }),
      duplex: "half",
    } as RequestInit & { duplex: "half" })

    const response = await handleBffMutation(oversized, {
      appUrl: "https://sourcing.example.com",
      path: "/api/v1/clients",
      method: "POST",
      schema: z.object({ name: z.string() }),
      readTenant: async () => tenantId,
      callApi,
    })

    expect(response.status).toBe(413)
    expect(callApi).not.toHaveBeenCalled()
  })
})

describe("BFF read and stream boundaries", () => {
  it("reads only through the signed selected tenant and sets no-store", async () => {
    const callApi = vi.fn().mockResolvedValue({ items: [], next_cursor: null })
    const response = await handleBffRead({
      path: "/api/v1/candidates?limit=25",
      readTenant: async () => tenantId,
      callApi,
    })

    expect(response.status).toBe(200)
    expect(response.headers.get("Cache-Control")).toBe("private, no-store")
    expect(callApi).toHaveBeenCalledWith(
      "/api/v1/candidates?limit=25",
      tenantId,
    )
  })

  it("streams CSV without buffering and forwards cancellation", async () => {
    let upstreamCancelled = false
    const upstream = new ReadableStream<Uint8Array>({
      pull(controller) {
        controller.enqueue(new TextEncoder().encode("candidate_id,name\r\n"))
      },
      cancel() {
        upstreamCancelled = true
      },
    })
    const response = await handleBffStream(
      new Request("https://sourcing.example.com/api/bff/jobs/job/export", {
        headers: {
          "Idempotency-Key": "export-intent",
          Origin: "https://sourcing.example.com",
          "Sec-Fetch-Site": "same-origin",
        },
      }),
      {
        appUrl: "https://sourcing.example.com",
        path: "/api/v1/jobs/00000000-0000-4000-8000-000000000101/export.csv",
        filename: "shortlist.csv",
        readTenant: async () => tenantId,
        callApi: vi.fn().mockResolvedValue(upstream),
      },
    )

    expect(response.headers.get("Cache-Control")).toBe("private, no-store")
    expect(response.headers.get("Content-Type")).toBe("text/csv; charset=utf-8")
    await response.body?.cancel()
    expect(upstreamCancelled).toBe(true)
  })

  it("rejects cross-origin export and a missing stable intent key", async () => {
    const callApi = vi.fn()
    const response = await handleBffStream(
      new Request("https://sourcing.example.com/api/bff/jobs/job/export", {
        headers: {
          Origin: "https://evil.example",
          "Sec-Fetch-Site": "cross-site",
        },
      }),
      {
        appUrl: "https://sourcing.example.com",
        path: "/api/v1/jobs/00000000-0000-4000-8000-000000000101/export.csv",
        filename: "shortlist.csv",
        readTenant: async () => tenantId,
        callApi,
      },
    )

    expect(response.status).toBe(403)
    expect(callApi).not.toHaveBeenCalled()
  })
})
