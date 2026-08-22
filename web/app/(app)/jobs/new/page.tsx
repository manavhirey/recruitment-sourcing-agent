import Link from "next/link"

import { JobIntakeForm } from "@/components/jobs/JobIntakeForm"
import { apiFetch } from "@/lib/api"
import { requirePageContext } from "@/lib/page-context"
import type { Client } from "@/lib/schemas"

export const metadata = { title: "New job" }

export default async function NewJobPage() {
  const context = await requirePageContext()
  const clients = await apiFetch<Client[]>("/api/v1/clients", context.tenantId)
  return (
    <div className="page-stack page-narrow">
      <Link className="back-link" href="/jobs">← Back to jobs</Link>
      <header className="page-header">
        <div>
          <p className="eyebrow">New search</p>
          <h1>Turn the brief into a scorecard</h1>
          <p>Start with the client’s exact requirements. You will review suggestions before sourcing.</p>
        </div>
      </header>
      {clients.length === 0 ? (
        <div className="empty-state" role="status">
          <h2>No authorized clients</h2>
          <p>Ask an agency manager for client access before creating a job.</p>
        </div>
      ) : <JobIntakeForm clients={clients} />}
    </div>
  )
}
