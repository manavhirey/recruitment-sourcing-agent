import { z } from "zod"

import { bffErrorResponse, handleBffRead } from "@/lib/bff"

const querySchema = z.object({
  q: z.string().max(255).optional(),
  location: z.string().max(255).optional(),
  industry: z.string().max(128).optional(),
  cursor: z.string().max(4_096).optional(),
  limit: z.coerce.number().int().min(1).max(100).default(50),
})

export async function GET(request: Request): Promise<Response> {
  const query = querySchema.safeParse(Object.fromEntries(new URL(request.url).searchParams))
  if (!query.success) return bffErrorResponse("validation_failed", 422)
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(query.data)) {
    if (value !== undefined && value !== "") search.set(key, String(value))
  }
  return handleBffRead({ path: `/api/v1/candidates?${search}` })
}
