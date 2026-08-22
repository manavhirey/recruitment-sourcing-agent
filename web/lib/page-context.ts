import "server-only"

import { redirect } from "next/navigation"

import { apiFetch, ApiError } from "@/lib/api"
import { auth, readServerJwt } from "@/lib/auth"
import { allowedTenantOptions, selectedTenantId } from "@/lib/tenant"
import { membershipValidatedTenantOptions } from "@/lib/tenant-options"
import type { JobPage, Me } from "@/lib/schemas"

export async function requirePageContext() {
  const session = await auth()
  if (!session) redirect("/api/auth/signin?callbackUrl=/jobs")
  const tenantId = await selectedTenantId()
  if (!tenantId) redirect("/select-tenant")

  let me: Me
  let jobs: JobPage
  try {
    ;[me, jobs] = await Promise.all([
      apiFetch<Me>("/api/v1/me", tenantId),
      apiFetch<JobPage>("/api/v1/jobs?limit=12&offset=0", tenantId),
    ])
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      redirect("/api/auth/signin?error=SessionExpired")
    }
    if (error instanceof ApiError && error.status === 404) {
      redirect("/select-tenant?error=access_changed")
    }
    throw error
  }
  const token = await readServerJwt()
  const tenantCandidates = allowedTenantOptions(
    token?.tenantOptions ?? [],
    process.env.TENANT_OPTIONS,
  )
  let tenantOptions
  try {
    tenantOptions = await membershipValidatedTenantOptions(
      tenantCandidates,
      async (candidateId) => {
        if (candidateId !== tenantId) await apiFetch("/api/v1/me", candidateId)
      },
    )
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      redirect("/api/auth/signin?error=SessionExpired")
    }
    throw error
  }
  const agency = tenantOptions.find((option) => option.id === tenantId)
  if (!agency) redirect("/select-tenant?error=configuration_changed")

  return { agency, jobs, me, session, tenantId, tenantOptions }
}
