"use client"

const ERROR_BODY_LIMIT = 8 * 1024
const SUCCESS_BODY_LIMIT = 2 * 1024 * 1024

export class ClientResponseError extends Error {
  constructor(
    readonly code: string,
    readonly status: number,
  ) {
    super(code)
  }
}

type ReauthRouter = { replace(href: string): void }

async function boundedText(response: Response, limit: number): Promise<string> {
  if (!response.body) return ""
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let size = 0
  let output = ""
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > limit) {
        await reader.cancel()
        throw new ClientResponseError("response_too_large", response.status)
      }
      output += decoder.decode(value, { stream: true })
    }
    return output + decoder.decode()
  } finally {
    reader.releaseLock()
  }
}

export async function requireResponse(response: Response): Promise<void> {
  if (response.ok) return
  let code = "request_failed"
  try {
    const body = JSON.parse(await boundedText(response, ERROR_BODY_LIMIT)) as {
      code?: unknown
    }
    if (typeof body.code === "string" && /^[a-z][a-z0-9_]*$/.test(body.code)) {
      code = body.code
    }
  } catch (error) {
    if (error instanceof ClientResponseError) throw error
    // Raw and malformed upstream errors are intentionally discarded.
  }
  throw new ClientResponseError(code, response.status)
}

export async function responseJson<T>(response: Response): Promise<T> {
  await requireResponse(response)
  try {
    return JSON.parse(await boundedText(response, SUCCESS_BODY_LIMIT)) as T
  } catch (error) {
    if (error instanceof ClientResponseError) throw error
    throw new ClientResponseError("invalid_response", response.status)
  }
}

export function reauthenticateExpiredSession(
  error: unknown,
  router: ReauthRouter,
): boolean {
  if (!(error instanceof ClientResponseError) || error.status !== 401) return false
  const callbackUrl = `${window.location.pathname}${window.location.search}`
  router.replace(
    `/api/auth/signin?callbackUrl=${encodeURIComponent(callbackUrl)}`,
  )
  return true
}
