import Link from "next/link"

import type { Client, JobSummary } from "@/lib/schemas"

export function JobList({
  jobs,
  clients,
}: {
  jobs: readonly JobSummary[]
  clients: readonly Client[]
}) {
  const clientNames = new Map(clients.map((client) => [client.id, client.name]))
  if (jobs.length === 0) {
    return (
      <div className="empty-state">
        <p className="eyebrow">Job pipeline</p>
        <h2>Your first search starts with a clear brief.</h2>
        <p>Create a job, review every inferred criterion, then start sourcing.</p>
        <Link className="button button-primary" href="/jobs/new">Create a job</Link>
      </div>
    )
  }
  return (
    <ul className="job-list" aria-label="Jobs">
      {jobs.map((job) => (
        <li key={job.id}>
          <Link href={`/jobs/${job.id}/scorecard`}>
            <div>
              <span className="status-dot" aria-hidden="true" />
              <strong>{job.title}</strong>
              <span>{clientNames.get(job.client_id) ?? "Authorized client"}</span>
            </div>
            <div className="job-meta">
              <span>{job.location ?? "Location open"}</span>
              <span className="status-pill">{job.status.replaceAll("_", " ")}</span>
            </div>
          </Link>
        </li>
      ))}
    </ul>
  )
}
