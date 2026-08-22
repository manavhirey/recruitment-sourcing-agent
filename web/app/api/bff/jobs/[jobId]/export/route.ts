import { z } from "zod"

import { bffErrorResponse, handleBffStream } from "@/lib/bff"
import { assertMutationOrigin } from "@/lib/tenant"

const noStoreHeaders = { "Cache-Control": "private, no-store" }

async function boundedFormBody(request: Request): Promise<string | Response> {
  const reader = request.body?.getReader()
  if (!reader) return ""
  const decoder = new TextDecoder("utf-8", { fatal: true })
  let bytes = 0
  let value = ""
  try {
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) return value + decoder.decode()
      bytes += chunk.value.byteLength
      if (bytes > 1_024) {
        await reader.cancel("request_too_large")
        return Response.json(
          { code: "request_too_large" },
          { status: 413, headers: noStoreHeaders },
        )
      }
      value += decoder.decode(chunk.value, { stream: true })
    }
  } catch {
    await reader.cancel().catch(() => undefined)
    return Response.json(
      { code: "request_invalid" },
      { status: 400, headers: noStoreHeaders },
    )
  }
}

export async function GET(
  request: Request,
  context: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  const jobId = z.uuid().safeParse((await context.params).jobId)
  if (!jobId.success) return bffErrorResponse("job_candidate_not_found", 404)
  const appUrl = process.env.AUTH_URL
  if (!appUrl) return bffErrorResponse("authentication_configuration_invalid", 503)
  return handleBffStream(request, {
    appUrl,
    path: `/api/v1/jobs/${jobId.data}/export.csv`,
    filename: `shortlist-${jobId.data}.csv`,
  })
}

export async function POST(
  request: Request,
  context: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  const declaredLength = Number(request.headers.get("Content-Length") ?? "0")
  const contentType = request.headers.get("Content-Type")?.toLowerCase() ?? ""
  const appUrl = process.env.AUTH_URL
  if (!appUrl) return Response.json({ code: "authentication_configuration_invalid" }, { status: 503, headers: noStoreHeaders })
  try {
    assertMutationOrigin(request, appUrl)
  } catch {
    return Response.json({ code: "invalid_request_origin" }, { status: 403, headers: noStoreHeaders })
  }
  if (
    declaredLength > 1_024 ||
    contentType.split(";", 1)[0]?.trim() !== "application/x-www-form-urlencoded"
  ) {
    return Response.json({ code: "request_invalid" }, { status: 400, headers: noStoreHeaders })
  }
  const raw = await boundedFormBody(request)
  if (raw instanceof Response) return raw
  const form = new URLSearchParams(raw)
  if ([...form.keys()].some((key) => key !== "idempotencyKey") || form.getAll("idempotencyKey").length !== 1) {
    return Response.json({ code: "request_invalid" }, { status: 400, headers: noStoreHeaders })
  }
  const idempotencyKey = form.get("idempotencyKey")?.trim()
  const headers = new Headers(request.headers)
  if (idempotencyKey) headers.set("Idempotency-Key", idempotencyKey)
  return GET(new Request(request.url, { headers, signal: request.signal }), context)
}
