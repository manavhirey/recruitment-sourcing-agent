import "server-only"

import { apiFetch, ApiError, type ApiInit } from "@/lib/api"
import { bffErrorResponse, bffPublicStatus } from "@/lib/bff"
import { assertMutationOrigin, selectedTenantId } from "@/lib/tenant"

const maximumFileBytes = 10_000_000
const supportedTypes = new Map([
  [".pdf", "application/pdf"],
  [
    ".docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ],
])

type DocumentExtractionDependencies = {
  appUrl: string
  readTenant?: () => Promise<string | null>
  callApi?: (
    path: string,
    tenantId: string,
    init: ApiInit,
  ) => Promise<unknown>
}

function hasSupportedType(file: File): boolean {
  const name = file.name.toLowerCase()
  const extension = [...supportedTypes.keys()].find((value) =>
    name.endsWith(value),
  )
  return (
    extension !== undefined &&
    file.type.toLowerCase() === supportedTypes.get(extension)
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

  let form: FormData
  try {
    form = await request.formData()
  } catch {
    return bffErrorResponse("job_description_file_required", 400)
  }
  if ([...form.keys()].some((key) => key !== "file")) {
    return bffErrorResponse("job_description_file_required", 400)
  }
  const values = form.getAll("file")
  if (values.length !== 1 || typeof values[0] === "string") {
    return bffErrorResponse("job_description_file_required", 400)
  }

  const file = values[0]
  if (file.size > maximumFileBytes) {
    return bffErrorResponse("job_description_file_too_large", 413)
  }
  if (!hasSupportedType(file)) {
    return bffErrorResponse("job_description_type_unsupported", 415)
  }

  const upstream = new FormData()
  upstream.set("file", file, file.name)

  let tenantId: string | null
  try {
    tenantId = await (dependencies.readTenant ?? selectedTenantId)()
  } catch {
    return bffErrorResponse("tenant_unavailable", 503)
  }
  if (!tenantId) return bffErrorResponse("tenant_required", 401)

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
      return bffErrorResponse(error.code, bffPublicStatus(error))
    }
    return bffErrorResponse("api_unavailable", 502)
  }
}
