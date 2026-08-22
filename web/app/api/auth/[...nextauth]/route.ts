import { authRouteHandlers } from "@/lib/auth"

const handlers = authRouteHandlers()

export const GET = handlers.GET
export const POST = handlers.POST
