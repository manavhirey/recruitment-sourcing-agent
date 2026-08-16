"use client"

import { useState } from "react"
import { useRouter } from "next/navigation"

import type { TenantOption } from "@/lib/auth-config"
import {
  reauthenticateExpiredSession,
  requireResponse,
} from "@/lib/client-response"

export function TenantChooser({ options }: { options: readonly TenantOption[] }) {
  const router = useRouter()
  const [tenantId, setTenantId] = useState(options[0]?.id ?? "")
  const [error, setError] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  return (
    <form
      className="tenant-form"
      onSubmit={async (event) => {
        event.preventDefault()
        if (!tenantId || submitting) return
        setSubmitting(true)
        setError(false)
        try {
          const response = await fetch("/api/tenant", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tenantId }),
          })
          await requireResponse(response)
          router.push("/jobs")
          router.refresh()
          return
        } catch (caught) {
          if (reauthenticateExpiredSession(caught, router)) return
          // Tenant verification errors are intentionally collapsed to a safe message.
        }
        setError(true)
        setSubmitting(false)
      }}
    >
      <label htmlFor="tenant-choice">Agency</label>
      <select id="tenant-choice" value={tenantId} onChange={(event) => setTenantId(event.target.value)}>
        {options.map((option) => <option key={option.id} value={option.id}>{option.name}</option>)}
      </select>
      {error ? <p className="form-error" role="alert">This agency could not be verified. Choose another or sign in again.</p> : null}
      <button className="button button-primary" type="submit" disabled={submitting}>
        {submitting ? "Verifying…" : "Continue"}
      </button>
    </form>
  )
}
