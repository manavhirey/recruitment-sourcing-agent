import { redirect } from "next/navigation"

import { TenantChooser } from "@/components/layout/TenantChooser"
import { apiFetch, ApiError } from "@/lib/api"
import { readServerJwt } from "@/lib/auth"
import { allowedTenantOptions } from "@/lib/tenant"
import { membershipValidatedTenantOptions } from "@/lib/tenant-options"

export const metadata = { title: "Choose agency" }

export default async function SelectTenantPage() {
  const token = await readServerJwt()
  if (!token) redirect("/api/auth/signin?callbackUrl=/select-tenant")
  const candidates = allowedTenantOptions(
    token.tenantOptions ?? [],
    process.env.TENANT_OPTIONS,
  )
  let options
  try {
    options = await membershipValidatedTenantOptions(
      candidates,
      async (tenantId) => {
        await apiFetch("/api/v1/me", tenantId)
      },
    )
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      redirect("/api/auth/signin?error=SessionExpired")
    }
    throw error
  }
  return (
    <main className="auth-page">
      <section className="auth-card">
        <div className="brand auth-brand"><span className="brand-mark" aria-hidden="true">N</span><span>Sourcing Desk</span></div>
        <p className="eyebrow">Secure workspace</p>
        <h1>Choose your agency</h1>
        <p>We verify membership with the API before opening any client data.</p>
        {options.length > 0 ? (
          <TenantChooser options={options} />
        ) : (
          <div className="manual-warning" role="alert">
            No verified agency options are configured for this account. Contact your administrator.
          </div>
        )}
      </section>
    </main>
  )
}
