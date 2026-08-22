import "server-only"

export class RequestBodyError extends Error {
  constructor(public readonly code: "request_invalid" | "request_too_large") {
    super(code)
    this.name = "RequestBodyError"
  }
}

export async function readBoundedJson(
  request: Request,
  maximumBytes: number,
): Promise<unknown> {
  const declaredLength = request.headers.get("Content-Length")
  if (declaredLength !== null) {
    const parsedLength = Number(declaredLength)
    if (!Number.isFinite(parsedLength) || parsedLength < 0) {
      throw new RequestBodyError("request_invalid")
    }
    if (parsedLength > maximumBytes) {
      throw new RequestBodyError("request_too_large")
    }
  }

  const reader = request.body?.getReader()
  if (!reader) throw new RequestBodyError("request_invalid")
  const decoder = new TextDecoder("utf-8", { fatal: true })
  let byteCount = 0
  let text = ""
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      byteCount += value.byteLength
      if (byteCount > maximumBytes) {
        await reader.cancel()
        throw new RequestBodyError("request_too_large")
      }
      text += decoder.decode(value, { stream: true })
    }
    text += decoder.decode()
    return JSON.parse(text) as unknown
  } catch (error) {
    if (error instanceof RequestBodyError) throw error
    throw new RequestBodyError("request_invalid")
  }
}
