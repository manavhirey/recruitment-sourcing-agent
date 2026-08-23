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

type BffReadDependencies = {
  path: string
  readTenant?: () => Promise<string | null>
  callApi?: (path: string, tenantId: string) => Promise<unknown>
}

type BffStreamDependencies = {
  appUrl: string
  path: string
  filename: string
  maximumBytes?: number
  readTenant?: () => Promise<string | null>
  callApi?: (
    path: string,
    tenantId: string,
    init: ApiInit,
  ) => Promise<ReadableStream<Uint8Array>>
}

export function bffErrorResponse(code: string, status: number): Response {
  return Response.json(
    { code },
    { status, headers: { "Cache-Control": "private, no-store" } },
  )
}

export function bffPublicStatus(error: ApiError): number {
  if (
    [400, 401, 403, 404, 409, 410, 413, 415, 422, 429, 503].includes(
      error.status,
    )
  ) {
    return error.status
  }
  return error.status === 504 ? 504 : 502
}

async function tenantFor(
  readTenant: (() => Promise<string | null>) | undefined,
): Promise<string | Response> {
  try {
    const tenantId = await (readTenant ?? selectedTenantId)()
    return tenantId ?? bffErrorResponse("tenant_required", 401)
  } catch {
    return bffErrorResponse("tenant_unavailable", 503)
  }
}

export async function handleBffRead(
  dependencies: BffReadDependencies,
): Promise<Response> {
  const tenant = await tenantFor(dependencies.readTenant)
  if (tenant instanceof Response) return tenant
  try {
    const result = await (dependencies.callApi ?? apiFetch)(
      dependencies.path,
      tenant,
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

export async function handleBffStream(
  request: Request,
  dependencies: BffStreamDependencies,
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
  const tenant = await tenantFor(dependencies.readTenant)
  if (tenant instanceof Response) return tenant
  try {
    const upstream = await (dependencies.callApi ?? apiFetch)(
      dependencies.path,
      tenant,
      {
        responseMode: "stream",
        idempotencyKey,
        signal: request.signal,
        timeoutMs: 30_000,
      },
    )
    const reader = upstream.getReader()
    const maximumBytes = dependencies.maximumBytes ?? 50 * 1024 * 1024
    let bytes = 0
    const bounded = new ReadableStream<Uint8Array>({
      async pull(controller) {
        try {
          const { done, value } = await reader.read()
          if (done) {
            controller.close()
            return
          }
          bytes += value.byteLength
          if (bytes > maximumBytes) {
            await reader.cancel("export_too_large")
            controller.error(new Error("export_too_large"))
            return
          }
          controller.enqueue(value)
        } catch {
          controller.error(new Error("export_interrupted"))
        }
      },
      async cancel(reason) {
        await reader.cancel(reason)
      },
    })
    return new Response(bounded, {
      headers: {
        "Cache-Control": "private, no-store",
        "Content-Disposition": `attachment; filename="${dependencies.filename}"`,
        "Content-Type": "text/csv; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
      },
    })
  } catch (error) {
    if (error instanceof ApiError) {
      return bffErrorResponse(error.code, bffPublicStatus(error))
    }
    return bffErrorResponse("api_unavailable", 502)
  }
}

export async function handleBffMutation(
  request: Request,
  dependencies: BffDependencies,
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
  if (!contentType.startsWith("application/json")) {
    return bffErrorResponse("content_type_invalid", 415)
  }
  let body: unknown
  try {
    body = await readBoundedJson(request, 128 * 1024)
  } catch (error) {
    if (error instanceof RequestBodyError && error.code === "request_too_large") {
      return bffErrorResponse("request_too_large", 413)
    }
    return bffErrorResponse("request_invalid", 400)
  }
  const parsed = dependencies.schema.safeParse(body)
  if (!parsed.success) return bffErrorResponse("validation_failed", 422)

  let tenantId: string | null
  try {
    tenantId = await (dependencies.readTenant ?? selectedTenantId)()
  } catch {
    return bffErrorResponse("tenant_unavailable", 503)
  }
  if (!tenantId) return bffErrorResponse("tenant_required", 401)
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
      ? new Response(null, {
          status: 204,
          headers: { "Cache-Control": "private, no-store" },
        })
      : Response.json(result, {
          headers: { "Cache-Control": "private, no-store" },
        })
  } catch (error) {
    if (error instanceof ApiError) {
      return bffErrorResponse(error.code, bffPublicStatus(error))
    }
    if (error instanceof Error && error.message === "unauthenticated") {
      return bffErrorResponse("unauthenticated", 401)
    }
    return bffErrorResponse("api_unavailable", 502)
  }
}

export async function bffMutation(
  request: Request,
  dependencies: Omit<BffDependencies, "appUrl">,
): Promise<Response> {
  const appUrl = process.env.AUTH_URL
  if (!appUrl) return bffErrorResponse("authentication_configuration_invalid", 503)
  return handleBffMutation(request, { ...dependencies, appUrl })
}
