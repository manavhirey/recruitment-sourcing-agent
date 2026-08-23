import Link from "next/link"
import { notFound } from "next/navigation"
import { z } from "zod"

import { ReviewWorkspace } from "@/components/candidates/ReviewWorkspace"
import type { WorkspaceTab } from "@/components/candidates/ReviewWorkspace"
import type { CandidateTableFilters } from "@/components/candidates/CandidateTable"
import { RunStatus } from "@/components/jobs/RunStatus"
import { apiFetch, ApiError } from "@/lib/api"
import { requirePageContext } from "@/lib/page-context"
import {
  isManager,
  type ConfirmedScorecard,
  type Job,
  type JobCandidate,
  type JobCandidatePage,
  type Member,
  type SourcingRun,
} from "@/lib/schemas"

export const metadata = { title: "Job review" }

export default async function JobWorkspacePage({
  params,
  searchParams,
}: {
  params: Promise<{ jobId: string }>
  searchParams: Promise<Record<string, string | string[] | undefined>>
}) {
  const parsedJobId = z.uuid().safeParse((await params).jobId)
  if (!parsedJobId.success) notFound()
  const context = await requirePageContext()
  const jobId = parsedJobId.data
  const rawSearch = await searchParams
  const value = (key: string) => typeof rawSearch[key] === "string" ? rawSearch[key] : undefined
  const tabResult = z.enum(["review", "all", "near", "scorecard", "activity"]).safeParse(value("tab"))
  const initialTab: WorkspaceTab = tabResult.success ? tabResult.data : "review"
  const stageResult = z.enum(["New", "Reviewed", "Shortlisted", "Rejected"]).safeParse(value("stage"))
  const contactResult = z.enum(["true", "false"]).safeParse(value("has_contact"))
  const sort: CandidateTableFilters["sort"] = value("sort") === "score" ? "score" : "-score"
  const cursorResult = z.string().min(1).max(4_096).regex(/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/).safeParse(value("cursor"))
  const tableFilters: CandidateTableFilters = {
    stage: stageResult.success ? stageResult.data : "",
    hasContact: contactResult.success ? contactResult.data : "",
    sort,
    cursor: cursorResult.success ? cursorResult.data : null,
  }
  let job: Job
  try {
    job = await apiFetch<Job>(`/api/v1/jobs/${jobId}`, context.tenantId)
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound()
    throw error
  }
  let run: SourcingRun | null = null
  try {
    run = await apiFetch<SourcingRun>(`/api/v1/jobs/${jobId}/runs/latest`, context.tenantId)
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 404)) throw error
  }
  if (!run) {
    return (
      <div className="page-stack">
        <header className="page-header"><div><p className="eyebrow">{job.status.replaceAll("_", " ")}</p><h1>{job.title}</h1><p>No sourcing run has started for this authorized job.</p></div></header>
        <div className="empty-state"><h2>Confirm the scorecard to source candidates.</h2><Link className="button button-primary" href={`/jobs/${job.id}/scorecard`}>Open scorecard</Link></div>
      </div>
    )
  }
  const [main, near, versions, members] = await Promise.all([
    apiFetch<JobCandidatePage>(`/api/v1/jobs/${jobId}/candidates?classification=main&sort=-score&limit=50`, context.tenantId),
    apiFetch<JobCandidatePage>(`/api/v1/jobs/${jobId}/candidates?classification=near_match&sort=-score&limit=50`, context.tenantId),
    apiFetch<ConfirmedScorecard[]>(`/api/v1/jobs/${jobId}/scorecards`, context.tenantId),
    isManager(context.me.role)
      ? apiFetch<Member[]>("/api/v1/members", context.tenantId)
      : Promise.resolve([]),
  ])
  let initialTablePage: JobCandidatePage | undefined
  if (initialTab === "all") {
    const tableSearch = new URLSearchParams({ classification: "main", sort, limit: "50" })
    if (tableFilters.stage) tableSearch.set("stage", tableFilters.stage)
    if (tableFilters.hasContact) tableSearch.set("has_contact", tableFilters.hasContact)
    if (tableFilters.cursor) tableSearch.set("cursor", tableFilters.cursor)
    try {
      initialTablePage = await apiFetch<JobCandidatePage>(`/api/v1/jobs/${jobId}/candidates?${tableSearch}`, context.tenantId)
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 400)) throw error
    }
  }
  const requestedCandidate = z.uuid().safeParse(value("candidate"))
  let selected: JobCandidate | null = null
  if (requestedCandidate.success) {
    try {
      const detail = await apiFetch<JobCandidate>(`/api/v1/job-candidates/${requestedCandidate.data}`, context.tenantId)
      if (detail.job_id === jobId) selected = detail
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 404)) throw error
    }
  }
  if (!selected && main.items[0]) {
    selected = await apiFetch<JobCandidate>(`/api/v1/job-candidates/${main.items[0].id}`, context.tenantId)
  }
  const scorecard = versions.find((version) => version.id === run.scorecard_version_id) ?? null
  const ownerOptions = isManager(context.me.role)
    ? members.filter((member) => member.active).map((member) => ({ userId: member.user_id, name: member.display_name }))
    : [{ userId: context.me.user_id, name: context.me.display_name }]
  return (
    <div className="page-stack workspace-page">
      <header className="page-header workspace-header"><div><p className="eyebrow">Candidate review</p><h1>{job.title}</h1><p>Evidence-backed matches from the immutable run scorecard.</p></div></header>
      <RunStatus jobId={jobId} initialRun={run} />
      <ReviewWorkspace
        jobId={jobId}
        runId={run.id}
        initialCandidates={main}
        initialSelectedCandidate={selected}
        initialNearMatches={near}
        immutableScorecard={scorecard}
        ownerOptions={ownerOptions}
        initialTab={initialTab}
        initialTablePage={initialTablePage}
        initialTableFilters={tableFilters}
      />
    </div>
  )
}
