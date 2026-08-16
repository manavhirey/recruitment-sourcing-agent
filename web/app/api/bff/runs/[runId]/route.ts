import { z } from "zod"

import { handleBffRead } from "@/lib/bff"

export async function GET(
  _request: Request,
  context: { params: Promise<{ runId: string }> },
): Promise<Response> {
  const runId = z.uuid().safeParse((await context.params).runId)
  if (!runId.success) return Response.json({ code: "run_not_found" }, { status: 404 })
  return handleBffRead({ path: `/api/v1/runs/${runId.data}` })
}
