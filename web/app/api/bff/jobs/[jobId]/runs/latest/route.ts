import { z } from "zod"

import { handleBffRead } from "@/lib/bff"

export async function GET(
  _request: Request,
  context: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  const jobId = z.uuid().safeParse((await context.params).jobId)
  if (!jobId.success) return Response.json({ code: "run_not_found" }, { status: 404 })
  return handleBffRead({ path: `/api/v1/jobs/${jobId.data}/runs/latest` })
}
