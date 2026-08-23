import { z } from "zod"

import { bffMutation } from "@/lib/bff"
import { clientGrantRequest } from "@/lib/request-schemas"

export async function POST(
  request: Request,
  context: { params: Promise<{ clientId: string }> },
): Promise<Response> {
  const clientId = z.uuid().safeParse((await context.params).clientId)
  if (!clientId.success) return Response.json({ code: "client_not_found" }, { status: 404 })
  return bffMutation(request, {
    path: `/api/v1/clients/${clientId.data}/grants`,
    method: "POST",
    schema: clientGrantRequest,
  })
}
