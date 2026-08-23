import { bffMutation } from "@/lib/bff"
import { clientCreateRequest } from "@/lib/request-schemas"

export async function POST(request: Request): Promise<Response> {
  return bffMutation(request, {
    path: "/api/v1/clients",
    method: "POST",
    schema: clientCreateRequest,
  })
}
