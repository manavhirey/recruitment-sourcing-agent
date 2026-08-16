import "server-only"

import type { z } from "zod"

import { apiFetch, ApiError, type ApiInit } from "@/lib/api"
import { readBoundedJson, RequestBodyError } from "@/lib/bounded-json"
import { assertMutationOrigin, selectedTenantId } from "@/lib/tenant"

type BffDependencies = {
  appUrl: string
  path: string
  method: "POST" | "PUT" | "PATCH" | "DELETE"
  schema: z.ZodType
  readTenant?: () => Promise<string | null>
  callApi?: (
    path: string,
    tenantId: string,
    init: ApiInit,
  ) => Promise<unknown>
}

function errorResponse(code: string, status: number): Response {
  return Response.json({ code }, { status })
}

export async function handleBffMutation(
  request: Request,
  dependencies: BffDependencies,
): Promise<Response> {
  try {
    assertMutationOrigin(request, dependencies.appUrl)
  } catch {
    return errorResponse("invalid_request_origin", 403)
  }
  const idempotencyKey = request.headers.get("Idempotency-Key")?.trim()
  if (!idempotencyKey || idempotencyKey.length > 200) {
    return errorResponse("idempotency_key_required", 400)
  }
  const contentType = request.headers.get("Content-Type")?.toLowerCase() ?? ""
  if (!contentType.startsWith("application/json")) {
    return errorResponse("content_type_invalid", 415)
  }
  let body: unknown
  try {
    body = await readBoundedJson(request, 128 * 1024)
  } catch (error) {
    if (error instanceof RequestBodyError && error.code === "request_too_large") {
      return errorResponse("request_too_large", 413)
    }
    return errorResponse("request_invalid", 400)
  }
  const parsed = dependencies.schema.safeParse(body)
  if (!parsed.success) return errorResponse("validation_failed", 422)

  let tenantId: string | null
  try {
    tenantId = await (dependencies.readTenant ?? selectedTenantId)()
  } catch {
    return errorResponse("tenant_unavailable", 503)
  }
  if (!tenantId) return errorResponse("tenant_required", 401)
  try {
    const result = await (dependencies.callApi ?? apiFetch)(
      dependencies.path,
      tenantId,
      {
        method: dependencies.method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(parsed.data),
        idempotencyKey,
      },
    )
    return result === undefined
      ? new Response(null, { status: 204 })
      : Response.json(result)
  } catch (error) {
    if (error instanceof ApiError) {
      const status = [400, 401, 403, 404, 409, 422, 429].includes(error.status)
        ? error.status
        : error.status === 504
          ? 504
          : 502
      return errorResponse(error.code, status)
    }
    if (error instanceof Error && error.message === "unauthenticated") {
      return errorResponse("unauthenticated", 401)
    }
    return errorResponse("api_unavailable", 502)
  }
}

export async function bffMutation(
  request: Request,
  dependencies: Omit<BffDependencies, "appUrl">,
): Promise<Response> {
  const appUrl = process.env.AUTH_URL
  if (!appUrl) return errorResponse("authentication_configuration_invalid", 503)
  return handleBffMutation(request, { ...dependencies, appUrl })
}
