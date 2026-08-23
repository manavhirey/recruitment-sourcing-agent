import { File as ServerFile } from "node:buffer"

import undici from "undici"
import { afterAll, describe, expect, it, vi } from "vitest"

import { POST } from "@/app/api/bff/job-descriptions/extract/route"
import { ApiError } from "@/lib/api"
import { handleDocumentExtraction } from "@/lib/document-extraction-bff"

const tenantId = "00000000-0000-4000-8000-000000000001"
const appUrl = "https://sourcing.example.com"
const docxMediaType =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

vi.stubGlobal("File", ServerFile)
vi.stubGlobal("FormData", undici.FormData)
vi.stubGlobal("Request", undici.Request)
afterAll(() => vi.unstubAllGlobals())

function uploadRequest(
  form: FormData,
  options: {
    idempotencyKey?: string | null
    origin?: string
    signal?: AbortSignal
  } = {},
): Request {
  const origin = options.origin ?? appUrl
  const headers = new Headers({
    Origin: origin,
    "Sec-Fetch-Site": origin === appUrl ? "same-origin" : "cross-site",
  })
  if (options.idempotencyKey !== null) {
    headers.set(
      "Idempotency-Key",
      options.idempotencyKey ?? "extract-intent",
    )
  }
  return new Request(`${appUrl}/api/bff/job-descriptions/extract`, {
    method: "POST",
    headers,
    body: form,
    signal: options.signal,
  })
}

function formWithFile(
  contents: BlobPart = "%PDF-1.4 safe",
  name = "role.pdf",
  type = "application/pdf",
): FormData {
  const form = new FormData()
  form.set("file", new File([contents], name, { type }))
  return form
}

async function expectError(
  response: Response,
  status: number,
  code: string,
): Promise<void> {
  expect(response.status).toBe(status)
  expect(response.headers.get("Cache-Control")).toBe("private, no-store")
  await expect(response.json()).resolves.toEqual({ code })
}

describe("document extraction BFF boundary", () => {
  it("returns a no-store 503 when the route has no configured application URL", async () => {
    const configuredUrl = process.env.AUTH_URL
    delete process.env.AUTH_URL
    try {
      const response = await POST(uploadRequest(formWithFile()))

      await expectError(
        response,
        503,
        "authentication_configuration_invalid",
      )
    } finally {
      if (configuredUrl === undefined) delete process.env.AUTH_URL
      else process.env.AUTH_URL = configuredUrl
    }
  })

  it("forwards exactly one bounded file as multipart without JSON encoding it", async () => {
    const responseBody = {
      text: "Senior Product Designer",
      source: { filename: "role.pdf", media_type: "application/pdf" },
    }
    const callApi = vi.fn().mockResolvedValue(responseBody)

    const response = await handleDocumentExtraction(
      uploadRequest(formWithFile()),
      {
        appUrl,
        readTenant: async () => tenantId,
        callApi,
      },
    )

    expect(response.status).toBe(200)
    expect(response.headers.get("Cache-Control")).toBe("private, no-store")
    await expect(response.json()).resolves.toEqual(responseBody)
    expect(callApi).toHaveBeenCalledOnce()
    expect(callApi).toHaveBeenCalledWith(
      "/api/v1/job-descriptions/extract",
      tenantId,
      expect.objectContaining({
        method: "POST",
        idempotencyKey: "extract-intent",
        timeoutMs: 12_000,
      }),
    )
    const init = callApi.mock.calls[0][2]
    expect(init.body).toBeInstanceOf(FormData)
    const forwarded = (init.body as FormData).getAll("file")
    expect(forwarded).toHaveLength(1)
    expect(forwarded[0]).toBeInstanceOf(File)
    expect((forwarded[0] as File).name).toBe("role.pdf")
    expect((forwarded[0] as File).type).toBe("application/pdf")
    expect(init.headers).toBeUndefined()
  })

  it("accepts the exact 10,000,000-byte file boundary", async () => {
    const callApi = vi.fn().mockResolvedValue({
      text: "Bounded role",
      source: { filename: "role.pdf", media_type: "application/pdf" },
    })

    const response = await handleDocumentExtraction(
      uploadRequest(formWithFile(new Uint8Array(10_000_000))),
      { appUrl, readTenant: async () => tenantId, callApi },
    )

    expect(response.status).toBe(200)
    const init = callApi.mock.calls[0][2]
    const forwarded = (init.body as FormData).get("file") as File
    expect(forwarded.size).toBe(10_000_000)
  })

  it("rejects a 10,000,001-byte file before tenant or upstream work", async () => {
    const readTenant = vi.fn(async () => tenantId)
    const callApi = vi.fn()

    const response = await handleDocumentExtraction(
      uploadRequest(formWithFile(new Uint8Array(10_000_001))),
      { appUrl, readTenant, callApi },
    )

    await expectError(response, 413, "job_description_file_too_large")
    expect(readTenant).not.toHaveBeenCalled()
    expect(callApi).not.toHaveBeenCalled()
  })

  it("rejects cross-origin uploads before parsing or upstream work", async () => {
    const callApi = vi.fn()
    const response = await handleDocumentExtraction(
      uploadRequest(formWithFile(), { origin: "https://evil.example" }),
      { appUrl, readTenant: async () => tenantId, callApi },
    )

    await expectError(response, 403, "invalid_request_origin")
    expect(callApi).not.toHaveBeenCalled()
  })

  it.each([
    ["missing", null],
    ["blank", "   "],
    ["over 200 characters", "x".repeat(201)],
  ])("rejects a %s idempotency key", async (_label, idempotencyKey) => {
    const callApi = vi.fn()
    const response = await handleDocumentExtraction(
      uploadRequest(formWithFile(), { idempotencyKey }),
      { appUrl, readTenant: async () => tenantId, callApi },
    )

    await expectError(response, 400, "idempotency_key_required")
    expect(callApi).not.toHaveBeenCalled()
  })

  it("forwards the trimmed stable idempotency key", async () => {
    const callApi = vi.fn().mockResolvedValue({
      text: "Role",
      source: { filename: "role.pdf", media_type: "application/pdf" },
    })
    await handleDocumentExtraction(
      uploadRequest(formWithFile(), { idempotencyKey: "  stable-intent  " }),
      { appUrl, readTenant: async () => tenantId, callApi },
    )

    expect(callApi.mock.calls[0][2].idempotencyKey).toBe("stable-intent")
  })

  it("rejects a missing selected tenant before the upstream call", async () => {
    const callApi = vi.fn()
    const response = await handleDocumentExtraction(
      uploadRequest(formWithFile()),
      {
        appUrl,
        readTenant: async () => null,
        callApi,
      },
    )

    await expectError(response, 401, "tenant_required")
    expect(callApi).not.toHaveBeenCalled()
  })

  it("returns a safe error when the selected tenant cannot be read", async () => {
    const callApi = vi.fn()
    const response = await handleDocumentExtraction(
      uploadRequest(formWithFile()),
      {
        appUrl,
        readTenant: async () => {
          throw new Error("private cookie detail")
        },
        callApi,
      },
    )

    await expectError(response, 503, "tenant_unavailable")
    expect(callApi).not.toHaveBeenCalled()
  })

  it.each([
    ["missing file", new FormData()],
    [
      "two files",
      (() => {
        const form = formWithFile()
        form.append(
          "file",
          new File(["%PDF-1.4"], "other.pdf", {
            type: "application/pdf",
          }),
        )
        return form
      })(),
    ],
    [
      "unexpected field",
      (() => {
        const form = formWithFile()
        form.set("job_id", "private-job")
        return form
      })(),
    ],
  ])("rejects an invalid multipart shape with %s", async (_label, form) => {
    const callApi = vi.fn()
    const response = await handleDocumentExtraction(uploadRequest(form), {
      appUrl,
      readTenant: async () => tenantId,
      callApi,
    })

    await expectError(response, 400, "job_description_file_required")
    expect(callApi).not.toHaveBeenCalled()
  })

  it.each([
    ["role.txt", "application/pdf"],
    ["role.pdf", "text/plain"],
    ["role.docx", "application/pdf"],
    ["role.pdf", docxMediaType],
  ])("rejects unsupported extension/type pair %s / %s", async (name, type) => {
    const callApi = vi.fn()
    const response = await handleDocumentExtraction(
      uploadRequest(formWithFile("document", name, type)),
      { appUrl, readTenant: async () => tenantId, callApi },
    )

    await expectError(response, 415, "job_description_type_unsupported")
    expect(callApi).not.toHaveBeenCalled()
  })

  it("rejects a non-multipart request", async () => {
    const callApi = vi.fn()
    const request = new Request(`${appUrl}/api/bff/job-descriptions/extract`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": "extract-intent",
        Origin: appUrl,
        "Sec-Fetch-Site": "same-origin",
      },
      body: JSON.stringify({ file: "not-a-file" }),
    })

    const response = await handleDocumentExtraction(request, {
      appUrl,
      readTenant: async () => tenantId,
      callApi,
    })

    await expectError(response, 415, "job_description_type_unsupported")
    expect(callApi).not.toHaveBeenCalled()
  })

  it.each([413, 415, 422, 503])(
    "preserves approved upstream status %i",
    async (status) => {
      const callApi = vi
        .fn()
        .mockRejectedValue(new ApiError(status, `upstream_${status}`))
      const response = await handleDocumentExtraction(
        uploadRequest(formWithFile()),
        {
          appUrl,
          readTenant: async () => tenantId,
          callApi,
        },
      )

      await expectError(response, status, `upstream_${status}`)
    },
  )

  it.each([
    [new ApiError(500, "private_backend_failure"), "private_backend_failure"],
    [new Error("private transport detail"), "api_unavailable"],
  ])("maps an unsafe upstream failure to 502", async (error, code) => {
    const response = await handleDocumentExtraction(
      uploadRequest(formWithFile()),
      {
        appUrl,
        readTenant: async () => tenantId,
        callApi: vi.fn().mockRejectedValue(error),
      },
    )

    await expectError(response, 502, code)
  })

  it("forwards the caller abort signal to the upstream request", async () => {
    const controller = new AbortController()
    const request = uploadRequest(formWithFile(), { signal: controller.signal })
    controller.abort("caller-left")
    const callApi = vi.fn().mockResolvedValue({
      text: "Role",
      source: { filename: "role.pdf", media_type: "application/pdf" },
    })

    await handleDocumentExtraction(request, {
      appUrl,
      readTenant: async () => tenantId,
      callApi,
    })

    const forwardedSignal = callApi.mock.calls[0][2].signal
    expect(forwardedSignal).toBe(request.signal)
    expect(forwardedSignal?.aborted).toBe(true)
  })
})
