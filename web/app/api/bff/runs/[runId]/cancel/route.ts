import { z } from "zod"

import { bffMutation } from "@/lib/bff"
import { emptyObjectRequest } from "@/lib/request-schemas"

export async function POST(
  request: Request,
  context: { params: Promise<{ runId: string }> },
): Promise<Response> {
  const runId = z.uuid().safeParse((await context.params).runId)
  if (!runId.success) return Response.json({ code: "run_not_found" }, { status: 404 })
  return bffMutation(request, {
    path: `/api/v1/runs/${runId.data}/cancel`,
    method: "POST",
    schema: emptyObjectRequest,
  })
}
