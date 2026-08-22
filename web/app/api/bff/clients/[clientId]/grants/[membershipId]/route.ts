import { z } from "zod"

import { bffMutation } from "@/lib/bff"
import { emptyObjectRequest } from "@/lib/request-schemas"

export async function DELETE(
  request: Request,
  context: { params: Promise<{ clientId: string; membershipId: string }> },
): Promise<Response> {
  const params = await context.params
  const clientId = z.uuid().safeParse(params.clientId)
  const membershipId = z.uuid().safeParse(params.membershipId)
  if (!clientId.success || !membershipId.success) {
    return Response.json({ code: "client_not_found" }, { status: 404 })
  }
  return bffMutation(request, {
    path: `/api/v1/clients/${clientId.data}/grants/${membershipId.data}`,
    method: "DELETE",
    schema: emptyObjectRequest,
  })
}
