import { notFound, redirect } from "next/navigation"
import { z } from "zod"

import { apiFetch, ApiError } from "@/lib/api"
import { requirePageContext } from "@/lib/page-context"
import type { SourcingRun } from "@/lib/schemas"

export default async function RunActivityLink({ searchParams }: { searchParams: Promise<{ run?: string }> }) {
  const runId = z.uuid().safeParse((await searchParams).run)
  if (!runId.success) notFound()
  const context = await requirePageContext()
  try {
    const run = await apiFetch<SourcingRun>(`/api/v1/runs/${runId.data}`, context.tenantId)
    redirect(`/jobs/${run.job_id}?tab=activity`)
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound()
    throw error
  }
}
