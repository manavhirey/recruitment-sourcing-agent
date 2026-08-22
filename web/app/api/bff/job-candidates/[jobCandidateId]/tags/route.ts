import { z } from "zod"

import { bffMutation } from "@/lib/bff"
import { tagsUpdateRequest } from "@/lib/request-schemas"

export async function PUT(
  request: Request,
  context: { params: Promise<{ jobCandidateId: string }> },
): Promise<Response> {
  const id = z.uuid().safeParse((await context.params).jobCandidateId)
  if (!id.success) return Response.json({ code: "job_candidate_not_found" }, { status: 404 })
  return bffMutation(request, {
    path: `/api/v1/job-candidates/${id.data}/tags`, method: "PUT", schema: tagsUpdateRequest,
  })
}
