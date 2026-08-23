import { z } from "zod"

import { bffMutation } from "@/lib/bff"
import { revisionRequest } from "@/lib/request-schemas"

export async function POST(
  request: Request,
  context: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  const jobId = z.uuid().safeParse((await context.params).jobId)
  if (!jobId.success) return Response.json({ code: "job_not_found" }, { status: 404 })
  return bffMutation(request, {
    path: `/api/v1/jobs/${jobId.data}/scorecard/confirm`,
    method: "POST",
    schema: revisionRequest,
  })
}
