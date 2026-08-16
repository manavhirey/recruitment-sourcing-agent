import { cookies } from "next/headers"

import { apiFetch } from "@/lib/api"
import { readServerAccessToken } from "@/lib/auth"
import {
  claimCapturedInvitation,
  invitationCookieSettings,
} from "@/lib/invitation-claim"
import type { components } from "@/lib/generated-api"
import { setSelectedTenant } from "@/lib/tenant"

export async function GET(request: Request): Promise<Response> {
  const secret = process.env.AUTH_SECRET
  if (!secret) {
    return Response.json(
      { code: "authentication_configuration_invalid" },
      { status: 503, headers: { "Cache-Control": "private, no-store" } },
    )
  }
  const environment = process.env.NODE_ENV ?? "development"
  const cookie = invitationCookieSettings(environment)
  const store = await cookies()
  return claimCapturedInvitation(request, {
    secret,
    environment,
    readCookie: () => store.get(cookie.name)?.value,
    hasSession: async () => {
      try {
        await readServerAccessToken()
        return true
      } catch (error) {
        if (error instanceof Error && error.message === "unauthenticated") return false
        throw error
      }
    },
    claim: async (token, tenantId, idempotencyKey) => apiFetch<components["schemas"]["MembershipResponse"]>(
      "/api/v1/membership-invitations/claim",
      tenantId,
      {
        method: "POST",
        idempotencyKey,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      },
    ),
    selectTenant: setSelectedTenant,
  })
}
