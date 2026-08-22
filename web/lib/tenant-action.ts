import "server-only"

import { z } from "zod"

import type { TenantOption } from "@/lib/auth-config"
import { ApiError } from "@/lib/api"
import { readBoundedJson, RequestBodyError } from "@/lib/bounded-json"
import { allowedTenantOptions, assertMutationOrigin } from "@/lib/tenant"

type TenantSelectionDependencies = {
  appUrl: string
  configuredOptions: string | undefined
  readVerifiedOptions: () => Promise<unknown>
  revalidateMembership: (tenantId: string) => Promise<void>
  writeTenant: (tenantId: string) => Promise<void>
}

const requestSchema = z.object({ tenantId: z.uuid() }).strict()

function errorResponse(code: string, status: number): Response {
  return Response.json({ code }, { status })
}

export async function handleTenantSelection(
  request: Request,
  dependencies: TenantSelectionDependencies,
): Promise<Response> {
  try {
    assertMutationOrigin(request, dependencies.appUrl)
  } catch {
    return errorResponse("invalid_request_origin", 403)
  }
  let body: unknown
  try {
    body = await readBoundedJson(request, 8 * 1024)
  } catch (error) {
    if (error instanceof RequestBodyError && error.code === "request_too_large") {
      return errorResponse("request_too_large", 413)
    }
    return errorResponse("request_invalid", 400)
  }
  const selection = requestSchema.safeParse(body)
  if (!selection.success) return errorResponse("tenant_selection_invalid", 400)

  let verified: unknown
  try {
    verified = await dependencies.readVerifiedOptions()
  } catch {
    return errorResponse("unauthenticated", 401)
  }
  let options: TenantOption[]
  try {
    options = allowedTenantOptions(verified, dependencies.configuredOptions)
  } catch {
    return errorResponse("tenant_options_configuration_invalid", 503)
  }
  if (options.length === 0) {
    return errorResponse("tenant_options_unavailable", 503)
  }
  if (!options.some((option) => option.id === selection.data.tenantId)) {
    return errorResponse("tenant_not_found", 404)
  }
  try {
    await dependencies.revalidateMembership(selection.data.tenantId)
  } catch (error) {
    if (error instanceof ApiError && [401, 404].includes(error.status)) {
      return errorResponse(error.status === 401 ? "unauthenticated" : "tenant_not_found", error.status)
    }
    return errorResponse("tenant_validation_unavailable", 503)
  }
  try {
    await dependencies.writeTenant(selection.data.tenantId)
  } catch {
    return errorResponse("tenant_selection_unavailable", 503)
  }
  return new Response(null, { status: 204 })
}
