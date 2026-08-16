import { describe, expect, it, vi } from "vitest"

import { ApiError, createApiFetcher } from "@/lib/api"

const tenantId = "00000000-0000-4000-8000-000000000001"

function apiWith(fetchImpl: typeof fetch) {
  return createApiFetcher({
    apiBaseUrl: "https://api.example.com",
    environment: "production",
    fetchImpl,
    getAccessToken: async () => "server-cookie-access-token",
  })
}

describe("server-only API client", () => {
  it("maps a locally expired provider token to the same 401 boundary", async () => {
    const fetcher = createApiFetcher({
      apiBaseUrl: "https://api.example.com",
      environment: "production",
      fetchImpl: vi.fn(),
      getAccessToken: async () => {
        throw new Error("unauthenticated")
      },
    })

    await expect(fetcher("/api/v1/me", tenantId)).rejects.toMatchObject({
      status: 401,
      code: "unauthenticated",
    })
  })

  it("attaches server credentials, tenant scope, no-store, and stable idempotency", async () => {
    const requests: Request[] = []
    const fetcher = apiWith(async (input, init) => {
      requests.push(new Request(input, init))
      return Response.json({ id: "job-1" }, { status: 201 })
    })

    await fetcher("/api/v1/jobs", tenantId, {
      method: "POST",
      body: JSON.stringify({ title: "Product Manager" }),
      idempotencyKey: "same-intent-key",
    })

    expect(requests).toHaveLength(1)
    expect(requests[0].url).toBe("https://api.example.com/api/v1/jobs")
    expect(requests[0].headers.get("Authorization")).toBe(
      "Bearer server-cookie-access-token",
    )
    expect(requests[0].headers.get("X-Tenant-ID")).toBe(tenantId)
    expect(requests[0].headers.get("Idempotency-Key")).toBe("same-intent-key")
    expect(requests[0].cache).toBe("no-store")
  })

  it.each([
    "https://evil.example/api/v1/jobs",
    "//evil.example/api/v1/jobs",
    "/api/v1/../health/ready",
    "/api/v1/%2e%2e/health/ready",
    "/api/v1/jobs\\escape",
    "/webhooks/apollo/token",
  ])("rejects path escape %s", async (path) => {
    const fetcher = apiWith(vi.fn())

    await expect(fetcher(path, tenantId)).rejects.toThrow("api_path_invalid")
  })

  it("requires a caller-supplied idempotency key for every mutation", async () => {
    const fetcher = apiWith(vi.fn())

    await expect(
      fetcher("/api/v1/jobs", tenantId, { method: "POST" }),
    ).rejects.toThrow("idempotency_key_required")
  })

  it("returns empty and stream responses without attempting JSON parsing", async () => {
    const emptyFetcher = apiWith(async () => new Response(null, { status: 204 }))
    const streamFetcher = apiWith(
      async () =>
        new Response("id,name\n1,Safe Candidate\n", {
          headers: { "Content-Type": "text/csv" },
        }),
    )

    await expect(emptyFetcher("/api/v1/me", tenantId)).resolves.toBeUndefined()
    const body = await streamFetcher<ReadableStream<Uint8Array>>(
      "/api/v1/jobs/job-1/export.csv",
      tenantId,
      { responseMode: "stream" },
    )
    expect(body).toBeInstanceOf(ReadableStream)
  })

  it("exposes only stable error metadata and never raw response text", async () => {
    const fetcher = apiWith(
      async () =>
        Response.json(
          {
            detail: { code: "tenant_not_found" },
            raw: "contact@example.com access-token-secret",
          },
          { status: 404 },
        ),
    )

    const error = await fetcher("/api/v1/clients", tenantId).catch(
      (reason: unknown) => reason,
    )

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 404, code: "tenant_not_found" })
    expect(String(error)).not.toContain("contact@example.com")
    expect(JSON.stringify(error)).not.toContain("access-token-secret")
  })

  it("aborts requests at the configured timeout", async () => {
    vi.useFakeTimers()
    const fetcher = apiWith((_input, init) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(new DOMException("Aborted", "AbortError")),
        )
      }),
    )

    const result = fetcher("/api/v1/me", tenantId, { timeoutMs: 25 })
    const rejection = expect(result).rejects.toThrow("api_timeout")
    await vi.advanceTimersByTimeAsync(25)

    await rejection
    vi.useRealTimers()
  })

  it("keeps the deadline active while a JSON response body is consumed", async () => {
    vi.useFakeTimers()
    const fetcher = apiWith(async (_input, init) => {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          init?.signal?.addEventListener("abort", () =>
            controller.error(new DOMException("private body detail", "AbortError")),
          )
        },
      })
      return new Response(body, { headers: { "Content-Type": "application/json" } })
    })

    const result = fetcher("/api/v1/me", tenantId, { timeoutMs: 25 })
    const rejection = expect(result).rejects.toThrow("api_timeout")
    await vi.advanceTimersByTimeAsync(25)
    await rejection
    vi.useRealTimers()
  })

  it("bounds JSON responses before parsing", async () => {
    const fetcher = apiWith(async () =>
      new Response(`{"value":"${"x".repeat(2 * 1024 * 1024)}"}`, {
        headers: { "Content-Type": "application/json" },
      }),
    )

    await expect(fetcher("/api/v1/me", tenantId)).rejects.toMatchObject({
      status: 502,
      code: "api_response_too_large",
    })
  })

  it("keeps the deadline through the lifetime of a streamed response", async () => {
    vi.useFakeTimers()
    const fetcher = apiWith(async (_input, init) => {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          init?.signal?.addEventListener("abort", () =>
            controller.error(new DOMException("private stream detail", "AbortError")),
          )
        },
      })
      return new Response(body, { headers: { "Content-Type": "text/csv" } })
    })

    const body = await fetcher<ReadableStream<Uint8Array>>(
      "/api/v1/jobs/job-1/export.csv",
      tenantId,
      { responseMode: "stream", timeoutMs: 25 },
    )
    const read = body.getReader().read()
    const rejection = expect(read).rejects.toThrow("api_timeout")
    await vi.advanceTimersByTimeAsync(25)
    await rejection
    vi.useRealTimers()
  })

  it("disables redirects even when a caller requests following them", async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.redirect).toBe("error")
      return Response.json({ ok: true })
    }) as typeof fetch

    await apiWith(fetchImpl)("/api/v1/me", tenantId, { redirect: "follow" })
    expect(fetchImpl).toHaveBeenCalledOnce()
  })
})
