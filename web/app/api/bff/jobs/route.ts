import { bffMutation } from "@/lib/bff"
import { jobCreateRequest } from "@/lib/request-schemas"

export async function POST(request: Request): Promise<Response> {
  return bffMutation(request, {
    path: "/api/v1/jobs",
    method: "POST",
    schema: jobCreateRequest,
  })
}
