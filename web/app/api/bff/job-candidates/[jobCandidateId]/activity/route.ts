import { z } from "zod"

import { handleBffRead } from "@/lib/bff"

export async function GET(
  request: Request,
  context: { params: Promise<{ jobCandidateId: string }> },
): Promise<Response> {
  const id = z.uuid().safeParse((await context.params).jobCandidateId)
  const url = new URL(request.url)
  const cursor = z.string().max(4_096).optional().safeParse(url.searchParams.get("cursor") ?? undefined)
  if (!id.success || !cursor.success) return Response.json({ code: "job_candidate_not_found" }, { status: 404 })
  const query = new URLSearchParams({ limit: "50" })
  if (cursor.data) query.set("cursor", cursor.data)
  return handleBffRead({ path: `/api/v1/job-candidates/${id.data}/activity?${query}` })
}
