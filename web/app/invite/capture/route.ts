import { z } from "zod"

import { readBoundedJson, RequestBodyError } from "@/lib/bounded-json"
import { captureInvitation } from "@/lib/invitation-claim"
import { assertMutationOrigin } from "@/lib/tenant"

const noStoreHeaders = {
  "Cache-Control": "private, no-store",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
}
const captureRequest = z.object({ token: z.string().max(128) }).strict()

function errorResponse(code: string, status: number): Response {
  return Response.json({ code }, { status, headers: noStoreHeaders })
}

export async function POST(request: Request): Promise<Response> {
  const appUrl = process.env.AUTH_URL
  const secret = process.env.AUTH_SECRET
  if (!appUrl || !secret) {
    return errorResponse("authentication_configuration_invalid", 503)
  }
  try {
    assertMutationOrigin(request, appUrl)
  } catch {
    return errorResponse("invalid_request_origin", 403)
  }
  const contentType = request.headers.get("Content-Type")?.toLowerCase() ?? ""
  if (contentType.split(";", 1)[0]?.trim() !== "application/json") {
    return errorResponse("content_type_invalid", 415)
  }
  let body: unknown
  try {
    body = await readBoundedJson(request, 512)
  } catch (error) {
    return errorResponse(
      error instanceof RequestBodyError && error.code === "request_too_large"
        ? "request_too_large"
        : "request_invalid",
      error instanceof RequestBodyError && error.code === "request_too_large"
        ? 413
        : 400,
    )
  }
  const parsed = captureRequest.safeParse(body)
  if (!parsed.success) return errorResponse("invitation_invalid", 400)
  return captureInvitation(parsed.data.token, {
    secret,
    environment: process.env.NODE_ENV ?? "development",
  })
}
