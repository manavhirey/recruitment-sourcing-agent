import { apiFetch } from "@/lib/api"
import { readServerJwt } from "@/lib/auth"
import { handleTenantSelection } from "@/lib/tenant-action"
import {
  assertMutationOrigin,
  clearSelectedTenant,
  setSelectedTenant,
} from "@/lib/tenant"

export async function POST(request: Request): Promise<Response> {
  const appUrl = process.env.AUTH_URL
  if (!appUrl) {
    return Response.json(
      { code: "authentication_configuration_invalid" },
      { status: 503 },
    )
  }
  return handleTenantSelection(request, {
    appUrl,
    configuredOptions: process.env.TENANT_OPTIONS,
    readVerifiedOptions: async () => {
      const token = await readServerJwt()
      if (!token) throw new Error("unauthenticated")
      return token.tenantOptions ?? []
    },
    revalidateMembership: async (tenantId) => {
      await apiFetch("/api/v1/clients", tenantId)
    },
    writeTenant: setSelectedTenant,
  })
}

export async function DELETE(request: Request): Promise<Response> {
  const appUrl = process.env.AUTH_URL
  if (!appUrl) {
    return Response.json(
      { code: "authentication_configuration_invalid" },
      { status: 503 },
    )
  }
  try {
    assertMutationOrigin(request, appUrl)
    if (!(await readServerJwt())) {
      return Response.json({ code: "unauthenticated" }, { status: 401 })
    }
    await clearSelectedTenant()
    return new Response(null, { status: 204 })
  } catch {
    return Response.json({ code: "invalid_request_origin" }, { status: 403 })
  }
}
