import { bffMutation } from "@/lib/bff"
import { invitationCreateRequest } from "@/lib/request-schemas"

export async function POST(request: Request): Promise<Response> {
  const response = await bffMutation(request, {
    path: "/api/v1/membership-invitations", method: "POST", schema: invitationCreateRequest,
  })
  response.headers.set("Cache-Control", "private, no-store")
  return response
}
