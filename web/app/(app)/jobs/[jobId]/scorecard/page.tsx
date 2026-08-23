import Link from "next/link"
import { notFound } from "next/navigation"
import { z } from "zod"

import { ScorecardEditor } from "@/components/scorecards/ScorecardEditor"
import { GenerateScorecardAction } from "@/components/scorecards/GenerateScorecardAction"
import { ManualSourceBrief } from "@/components/scorecards/ManualSourceBrief"
import { apiFetch, ApiError } from "@/lib/api"
import { requirePageContext } from "@/lib/page-context"
import type { Client, Job, ScorecardDraftResponse } from "@/lib/schemas"

export const metadata = { title: "Review scorecard" }

export default async function ScorecardPage({
  params,
}: {
  params: Promise<{ jobId: string }>
}) {
  const parsed = z.uuid().safeParse((await params).jobId)
  if (!parsed.success) notFound()
  const context = await requirePageContext()
  let job: Job
  try {
    job = await apiFetch<Job>(`/api/v1/jobs/${parsed.data}`, context.tenantId)
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound()
    throw error
  }
  if (job.draft_revision === 0) {
    return (
      <div className="page-stack">
        <Link className="back-link" href="/jobs">← Back to jobs</Link>
        <header className="page-header scorecard-heading">
          <div>
            <p className="eyebrow">Scorecard review</p>
            <h1>{job.title}</h1>
            <p>The job is saved. Resume the scorecard generation step here.</p>
          </div>
        </header>
        <GenerateScorecardAction jobId={job.id} expectedRevision={0} />
      </div>
    )
  }
  let draft: ScorecardDraftResponse
  let client: Client
  try {
    ;[draft, client] = await Promise.all([
      apiFetch<ScorecardDraftResponse>(
        `/api/v1/jobs/${parsed.data}/scorecard/draft`,
        context.tenantId,
      ),
      apiFetch<Client>(`/api/v1/clients/${job.client_id}`, context.tenantId),
    ])
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound()
    throw error
  }
  const editableDraft = {
    job_id: draft.job_id,
    draft_revision: draft.draft_revision,
    draft: draft.draft,
    extraction_status: draft.extraction_status,
    extraction_warning: draft.extraction_warning,
  }
  return (
    <div className="page-stack">
      <Link className="back-link" href="/jobs">← Back to jobs</Link>
      <header className="page-header scorecard-heading">
        <div>
          <p className="eyebrow">Scorecard review</p>
          <h1>{job.title}</h1>
          <p>Confirm or delete every inferred item. Confirmed versions cannot be edited.</p>
        </div>
        <span className="revision-badge">Draft revision {draft.draft_revision}</span>
      </header>
      {draft.extraction_status === "manual_required" ? (
        <ManualSourceBrief jobDescription={draft.original_job_description} />
      ) : null}
      <ScorecardEditor
        draft={editableDraft}
        allowedIndustryCodes={client.industry_codes}
        alreadyConfirmed={job.current_scorecard_id !== null}
      />
    </div>
  )
}
