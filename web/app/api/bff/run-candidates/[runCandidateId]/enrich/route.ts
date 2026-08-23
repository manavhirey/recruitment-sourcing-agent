import { z } from "zod"

import { bffMutation } from "@/lib/bff"
import { emptyObjectRequest } from "@/lib/request-schemas"

export async function POST(
  request: Request,
  context: { params: Promise<{ runCandidateId: string }> },
): Promise<Response> {
  const id = z.uuid().safeParse((await context.params).runCandidateId)
  if (!id.success) return Response.json({ code: "run_candidate_not_found" }, { status: 404 })
  return bffMutation(request, {
    path: `/api/v1/job-candidates/${id.data}/enrich`, method: "POST", schema: emptyObjectRequest,
  })
}
