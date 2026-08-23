import "server-only"

import { apiFetch, ApiError, type ApiInit } from "@/lib/api"
import { bffErrorResponse } from "@/lib/bff"
import {
  MultipartUploadError,
  readMultipartUpload,
} from "@/lib/multipart-upload"
import { assertMutationOrigin, selectedTenantId } from "@/lib/tenant"

const maximumFileBytes = 10_000_000
const supportedTypes = new Map([
  [".pdf", "application/pdf"],
  [
    ".docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ],
])
const publicUpstreamErrors = new Map<string, number>([
  ["job_description_file_required", 400],
  ["tenant_required", 400],
  ["invalid_token", 401],
  ["unauthenticated", 401],
  ["forbidden", 403],
  ["tenant_not_found", 404],
  ["job_description_file_too_large", 413],
  ["job_description_type_unsupported", 415],
  ["job_description_file_unreadable", 422],
  ["job_description_text_missing", 422],
  ["job_description_text_too_long", 422],
  ["job_description_file_too_complex", 422],
  ["authentication_unavailable", 503],
  ["job_description_extraction_unavailable", 503],
  ["api_unavailable", 502],
])

type DocumentExtractionDependencies = {
  appUrl: string
  authenticate?: () => Promise<void>
  readTenant?: () => Promise<string | null>
  callApi?: (
    path: string,
    tenantId: string,
    init: ApiInit,
  ) => Promise<unknown>
}

async function authenticateRequest(): Promise<void> {
  const { readServerAccessToken } = await import("@/lib/auth")
  await readServerAccessToken()
}

function hasSupportedType(filename: string, mediaType: string): boolean {
  const name = filename.toLowerCase()
  const extension = [...supportedTypes.keys()].find((value) =>
    name.endsWith(value),
  )
  return (
    extension !== undefined &&
    mediaType.toLowerCase() === supportedTypes.get(extension)
  )
}

export async function handleDocumentExtraction(
  request: Request,
  dependencies: DocumentExtractionDependencies,
): Promise<Response> {
  try {
    assertMutationOrigin(request, dependencies.appUrl)
  } catch {
    return bffErrorResponse("invalid_request_origin", 403)
  }

  const idempotencyKey = request.headers.get("Idempotency-Key")?.trim()
  if (!idempotencyKey || idempotencyKey.length > 200) {
    return bffErrorResponse("idempotency_key_required", 400)
  }

  const contentType = request.headers.get("Content-Type")?.toLowerCase() ?? ""
  if (!contentType.startsWith("multipart/form-data")) {
    return bffErrorResponse("job_description_type_unsupported", 415)
  }

  const authenticate = dependencies.authenticate ?? (
    dependencies.callApi === undefined ? authenticateRequest : undefined
  )
  if (authenticate) {
    try {
      await authenticate()
    } catch (error) {
      if (error instanceof Error && error.message === "unauthenticated") {
        return bffErrorResponse("unauthenticated", 401)
      }
      return bffErrorResponse("authentication_unavailable", 503)
    }
  }

  let tenantId: string | null
  try {
    tenantId = await (dependencies.readTenant ?? selectedTenantId)()
  } catch {
    return bffErrorResponse("tenant_unavailable", 503)
  }
  if (!tenantId) return bffErrorResponse("tenant_required", 401)

  let upload
  try {
    upload = await readMultipartUpload(request)
  } catch (error) {
    if (error instanceof MultipartUploadError) {
      const status = error.code === "job_description_file_too_large" ? 413 : 400
      return bffErrorResponse(error.code, status)
    }
    return bffErrorResponse("job_description_file_required", 400)
  }
  if (upload.contents.byteLength > maximumFileBytes) {
    return bffErrorResponse("job_description_file_too_large", 413)
  }
  if (!hasSupportedType(upload.filename, upload.mediaType)) {
    return bffErrorResponse("job_description_type_unsupported", 415)
  }

  const file = new File(
    [Uint8Array.from(upload.contents).buffer],
    upload.filename,
    { type: upload.mediaType },
  )
  const upstream = new FormData()
  upstream.set("file", file, upload.filename)

  try {
    const result = await (dependencies.callApi ?? apiFetch)(
      "/api/v1/job-descriptions/extract",
      tenantId,
      {
        method: "POST",
        body: upstream,
        idempotencyKey,
        signal: request.signal,
        timeoutMs: 12_000,
      },
    )
    return Response.json(result, {
      headers: { "Cache-Control": "private, no-store" },
    })
  } catch (error) {
    if (error instanceof ApiError) {
      const expectedStatus = publicUpstreamErrors.get(error.code)
      if (expectedStatus === error.status) {
        return bffErrorResponse(error.code, expectedStatus)
      }
    }
    return bffErrorResponse("api_unavailable", 502)
  }
}
