import { z } from "zod"

import { bffMutation } from "@/lib/bff"
import { roleUpdateRequest } from "@/lib/request-schemas"

export async function PATCH(
  request: Request,
  context: { params: Promise<{ membershipId: string }> },
): Promise<Response> {
  const id = z.uuid().safeParse((await context.params).membershipId)
  if (!id.success) return Response.json({ code: "member_not_found" }, { status: 404 })
  return bffMutation(request, {
    path: `/api/v1/members/${id.data}/role`, method: "PATCH", schema: roleUpdateRequest,
  })
}
