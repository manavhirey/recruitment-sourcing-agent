import { z } from "zod"

import { handleBffRead } from "@/lib/bff"

export async function GET(
  _request: Request,
  context: { params: Promise<{ candidateId: string }> },
): Promise<Response> {
  const id = z.uuid().safeParse((await context.params).candidateId)
  if (!id.success) return Response.json({ code: "candidate_not_found" }, { status: 404 })
  return handleBffRead({ path: `/api/v1/candidates/${id.data}/jobs` })
}
