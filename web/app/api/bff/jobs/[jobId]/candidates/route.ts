import { z } from "zod"

import { bffErrorResponse, handleBffRead } from "@/lib/bff"

const querySchema = z.object({
  classification: z.enum(["main", "near_match"]).default("main"),
  sort: z.enum(["-score", "score"]).default("-score"),
  score_min: z.coerce.number().int().min(0).max(100).optional(),
  score_max: z.coerce.number().int().min(0).max(100).optional(),
  stage: z.enum(["New", "Reviewed", "Shortlisted", "Rejected"]).optional(),
  owner: z.uuid().optional(),
  tags: z.string().max(1_700).optional(),
  location: z.string().max(255).optional(),
  industry: z.string().max(128).optional(),
  has_contact: z.enum(["true", "false"]).optional(),
  q: z.string().max(255).optional(),
  cursor: z.string().max(4_096).optional(),
  limit: z.coerce.number().int().min(1).max(100).default(50),
})

export async function GET(
  request: Request,
  context: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  const jobId = z.uuid().safeParse((await context.params).jobId)
  const values = Object.fromEntries(new URL(request.url).searchParams)
  const query = querySchema.safeParse(values)
  if (!jobId.success || !query.success) {
    return bffErrorResponse("job_candidate_not_found", 404)
  }
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(query.data)) {
    if (value !== undefined) search.set(key, String(value))
  }
  return handleBffRead({
    path: `/api/v1/jobs/${jobId.data}/candidates?${search.toString()}`,
  })
}
