import { z } from "zod"

import { handleBffRead } from "@/lib/bff"

export async function GET(
  _request: Request,
  context: { params: Promise<{ jobCandidateId: string }> },
): Promise<Response> {
  const id = z.uuid().safeParse((await context.params).jobCandidateId)
  if (!id.success) return Response.json({ code: "job_candidate_not_found" }, { status: 404 })
  return handleBffRead({ path: `/api/v1/job-candidates/${id.data}` })
}
