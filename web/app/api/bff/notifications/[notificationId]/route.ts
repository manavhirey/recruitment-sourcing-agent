import { z } from "zod"

import { bffMutation } from "@/lib/bff"
import { emptyObjectRequest } from "@/lib/request-schemas"

export async function PATCH(
  request: Request,
  context: { params: Promise<{ notificationId: string }> },
): Promise<Response> {
  const id = z.uuid().safeParse((await context.params).notificationId)
  if (!id.success) return Response.json({ code: "notification_not_found" }, { status: 404 })
  return bffMutation(request, {
    path: `/api/v1/notifications/${id.data}`, method: "PATCH", schema: emptyObjectRequest,
  })
}
