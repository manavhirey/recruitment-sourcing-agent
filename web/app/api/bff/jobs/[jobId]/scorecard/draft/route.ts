import { z } from "zod"

import { bffMutation } from "@/lib/bff"
import { scorecardDraftUpdateRequest } from "@/lib/request-schemas"

export async function PUT(
  request: Request,
  context: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  const jobId = z.uuid().safeParse((await context.params).jobId)
  if (!jobId.success) return Response.json({ code: "job_not_found" }, { status: 404 })
  return bffMutation(request, {
    path: `/api/v1/jobs/${jobId.data}/scorecard/draft`,
    method: "PUT",
    schema: scorecardDraftUpdateRequest,
  })
}
