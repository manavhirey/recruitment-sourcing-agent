import Link from "next/link"

import { JobList } from "@/components/jobs/JobList"
import { apiFetch } from "@/lib/api"
import { requirePageContext } from "@/lib/page-context"
import type { Client, JobPage } from "@/lib/schemas"

export const metadata = { title: "Jobs" }

export default async function JobsPage() {
  const context = await requirePageContext()
  const [jobs, clients] = await Promise.all([
    apiFetch<JobPage>("/api/v1/jobs?limit=50&offset=0", context.tenantId),
    apiFetch<Client[]>("/api/v1/clients", context.tenantId),
  ])
  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Agency workspace</p>
          <h1>Jobs</h1>
          <p>Move from client brief to a confirmed, immutable search strategy.</p>
        </div>
        <Link className="button button-primary" href="/jobs/new">New job</Link>
      </header>
      <JobList jobs={jobs.items} clients={clients} />
    </div>
  )
}
