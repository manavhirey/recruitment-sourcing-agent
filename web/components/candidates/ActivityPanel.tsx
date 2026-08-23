"use client"

import { useQuery } from "@tanstack/react-query"

import { responseJson } from "@/lib/client-response"
import type { CandidateActivityPage, RunActivity } from "@/lib/schemas"

const actionLabels: Record<string, string> = {
  "sourcing_run.started": "Sourcing started",
  "sourcing_run.cancelled": "Sourcing cancelled",
  "sourcing_run.usage_budget_exhausted": "Usage budget reached",
  "sourcing_run.planned": "Provider search planned",
  "sourcing_run.source_completed": "Candidate sourcing completed",
  "sourcing_run.matched": "Candidate matching completed",
  "sourcing_run.enrichment_applied": "Candidate enrichment updated",
  "candidate.match_materialized": "Candidate matched",
  "candidate.match_rescored": "Candidate rescored",
  "candidate.stage_changed": "Review stage changed",
  "candidate.note_added": "Note added",
  "candidate.owner_changed": "Owner changed",
  "candidate.tags_changed": "Tags changed",
  "candidate.contact_revealed": "Contact revealed",
  "candidate.enrichment_queued": "Contact enrichment queued",
  "candidate.shortlist_exported": "Shortlist exported",
}

function presentation(action: string): string | null {
  return actionLabels[action] ?? null
}

export function ActivityPanel({
  runId,
  jobCandidateId,
}: {
  runId?: string | null
  jobCandidateId?: string | null
}) {
  const runQuery = useQuery({
    queryKey: ["run-activity", runId],
    enabled: Boolean(runId),
    queryFn: async () => responseJson<RunActivity[]>(await fetch(`/api/bff/runs/${runId}/activity`)),
  })
  const candidateQuery = useQuery({
    queryKey: ["candidate-activity", jobCandidateId],
    enabled: Boolean(jobCandidateId),
    queryFn: async () => responseJson<CandidateActivityPage>(await fetch(`/api/bff/job-candidates/${jobCandidateId}/activity`)),
  })
  const events = [
    ...(runQuery.data ?? [])
      .filter((event) => !jobCandidateId || !event.action.startsWith("candidate."))
      .map((event) => ({ id: event.id, action: event.action, summary: event.summary, created_at: event.created_at })),
    ...(candidateQuery.data?.items ?? []).map((event) => ({ id: event.id, action: event.action, summary: null, created_at: event.created_at })),
  ].filter((event) => presentation(event.action))
    .sort((a, b) => b.created_at.localeCompare(a.created_at))

  if (
    (Boolean(runId) && runQuery.isPending) ||
    (Boolean(jobCandidateId) && candidateQuery.isPending)
  ) return <p role="status">Loading activity…</p>
  if (
    (Boolean(runId) && runQuery.isError) ||
    (Boolean(jobCandidateId) && candidateQuery.isError)
  ) return <p className="form-error" role="alert">Activity is temporarily unavailable.</p>
  if (events.length === 0) return <div className="empty-state compact"><h2>No activity yet</h2><p>Allowlisted sourcing and review actions will appear here.</p></div>
  return (
    <ol className="activity-list">
      {events.map((event) => (
        <li key={event.id}>
          <strong>{presentation(event.action)}</strong>
          {event.summary ? <span>{event.summary}</span> : null}
          <time dateTime={event.created_at}>{new Date(event.created_at).toLocaleString()}</time>
        </li>
      ))}
    </ol>
  )
}
