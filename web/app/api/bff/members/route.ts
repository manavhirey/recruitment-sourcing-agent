import { handleBffRead } from "@/lib/bff"

export async function GET(): Promise<Response> {
  return handleBffRead({ path: "/api/v1/members" })
}
