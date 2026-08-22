import { z } from "zod"

import { bffMutation } from "@/lib/bff"
import { emptyObjectRequest } from "@/lib/request-schemas"

export async function POST(
  request: Request,
  context: { params: Promise<{ contactPointId: string }> },
): Promise<Response> {
  const id = z.uuid().safeParse((await context.params).contactPointId)
  if (!id.success) return Response.json({ code: "contact_point_not_found" }, { status: 404 })
  const response = await bffMutation(request, {
    path: `/api/v1/contact-points/${id.data}/reveal`, method: "POST", schema: emptyObjectRequest,
  })
  response.headers.set("Cache-Control", "private, no-store")
  return response
}
