import "server-only"

export type ApiInit = Omit<RequestInit, "cache"> & {
  idempotencyKey?: string
  responseMode?: "json" | "stream"
  timeoutMs?: number
}

type ApiFetcherDependencies = {
  apiBaseUrl: string
  environment: string
  fetchImpl: typeof fetch
  getAccessToken: () => Promise<string>
}

const tenantIdPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const stableCodePattern = /^[a-z][a-z0-9_]{0,63}$/
const maximumJsonResponseBytes = 1024 * 1024
const maximumErrorResponseBytes = 64 * 1024

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
  ) {
    super(code)
    this.name = "ApiError"
  }
}

function apiBaseUrl(value: string, environment: string): URL {
  let url: URL
  try {
    url = new URL(value)
  } catch {
    throw new Error("api_base_url_invalid")
  }
  const localDevelopment =
    environment !== "production" &&
    url.protocol === "http:" &&
    ["localhost", "127.0.0.1", "::1"].includes(url.hostname)
  if (
    (url.protocol !== "https:" && !localDevelopment) ||
    url.username ||
    url.password ||
    url.pathname !== "/" ||
    url.search ||
    url.hash
  ) {
    throw new Error("api_base_url_invalid")
  }
  return url
}

function apiUrl(base: URL, path: string): URL {
  if (
    !/^\/api\/v1(?:\/|$)/.test(path) ||
    path.startsWith("//") ||
    /[\\#\r\n]/.test(path) ||
    /%(?:2e|2f|5c)/i.test(path)
  ) {
    throw new Error("api_path_invalid")
  }
  const url = new URL(path, base)
  const segments = url.pathname.split("/")
  if (
    url.origin !== base.origin ||
    !url.pathname.startsWith("/api/v1/") && url.pathname !== "/api/v1" ||
    url.pathname.includes("//") ||
    segments.some((segment) => segment === "." || segment === "..")
  ) {
    throw new Error("api_path_invalid")
  }
  return url
}

function safeErrorCode(payload: unknown): string {
  if (!payload || typeof payload !== "object") return "api_request_failed"
  const detail = (payload as Record<string, unknown>).detail
  if (!detail || typeof detail !== "object") return "api_request_failed"
  const code = (detail as Record<string, unknown>).code
  return typeof code === "string" && stableCodePattern.test(code)
    ? code
    : "api_request_failed"
}

async function readBoundedBody(
  response: Response,
  maximumBytes: number,
  abort: () => void,
): Promise<string> {
  const declaredLength = response.headers.get("content-length")
  if (declaredLength !== null && Number(declaredLength) > maximumBytes) {
    abort()
    throw new ApiError(502, "api_response_too_large")
  }
  const reader = response.body?.getReader()
  if (!reader) return ""
  const decoder = new TextDecoder("utf-8", { fatal: true })
  let byteCount = 0
  let text = ""
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    byteCount += value.byteLength
    if (byteCount > maximumBytes) {
      await reader.cancel()
      abort()
      throw new ApiError(502, "api_response_too_large")
    }
    text += decoder.decode(value, { stream: true })
  }
  return text + decoder.decode()
}

export function createApiFetcher(dependencies: ApiFetcherDependencies) {
  const base = apiBaseUrl(
    dependencies.apiBaseUrl,
    dependencies.environment,
  )

  return async function fetchFromApi<T>(
    path: string,
    tenantId: string,
    init: ApiInit = {},
  ): Promise<T> {
    if (!tenantIdPattern.test(tenantId)) throw new Error("tenant_id_invalid")
    const url = apiUrl(base, path)
    const method = (init.method ?? "GET").toUpperCase()
    const mutation = method !== "GET" && method !== "HEAD"
    if (mutation && !init.idempotencyKey) {
      throw new Error("idempotency_key_required")
    }
    if (
      init.idempotencyKey &&
      (init.idempotencyKey.length > 200 || !init.idempotencyKey.trim())
    ) {
      throw new Error("idempotency_key_invalid")
    }

    let token: string
    try {
      token = await dependencies.getAccessToken()
    } catch (error) {
      if (error instanceof Error && error.message === "unauthenticated") {
        throw new ApiError(401, "unauthenticated")
      }
      throw new ApiError(503, "authentication_unavailable")
    }
    const headers = new Headers(init.headers)
    headers.set("Authorization", `Bearer ${token}`)
    headers.set("X-Tenant-ID", tenantId)
    headers.set("Accept", init.responseMode === "stream" ? "*/*" : "application/json")
    if (mutation) headers.set("Idempotency-Key", init.idempotencyKey!)
    const timeoutMs = Math.min(Math.max(init.timeoutMs ?? 15_000, 1), 30_000)
    const controller = new AbortController()
    let timedOut = false
    const onCallerAbort = () => controller.abort(init.signal?.reason)
    if (init.signal?.aborted) controller.abort(init.signal.reason)
    else init.signal?.addEventListener("abort", onCallerAbort, { once: true })
    const timeout = setTimeout(() => {
      timedOut = true
      controller.abort()
    }, timeoutMs)
    const requestInit: RequestInit = { ...init }
    delete (requestInit as Partial<ApiInit>).idempotencyKey
    delete (requestInit as Partial<ApiInit>).responseMode
    delete (requestInit as Partial<ApiInit>).timeoutMs
    let response: Response
    let cleanedUp = false
    const cleanup = () => {
      if (cleanedUp) return
      cleanedUp = true
      clearTimeout(timeout)
      init.signal?.removeEventListener("abort", onCallerAbort)
    }
    const transportError = () => {
      if (timedOut) return new ApiError(504, "api_timeout")
      if (controller.signal.aborted) return new ApiError(499, "api_aborted")
      return new ApiError(502, "api_unavailable")
    }
    try {
      response = await dependencies.fetchImpl(url, {
        ...requestInit,
        method,
        headers,
        cache: "no-store",
        signal: controller.signal,
        redirect: "error",
      })
    } catch {
      cleanup()
      throw transportError()
    }

    const wrapStream = (body: ReadableStream<Uint8Array>) => {
      const reader = body.getReader()
      return new ReadableStream<Uint8Array>({
        async pull(streamController) {
          try {
            const { done, value } = await reader.read()
            if (done) {
              cleanup()
              streamController.close()
            } else {
              streamController.enqueue(value)
            }
          } catch {
            cleanup()
            streamController.error(transportError())
          }
        },
        async cancel(reason) {
          controller.abort(reason)
          cleanup()
          await reader.cancel(reason)
        },
      })
    }

    try {
      const contentType = response.headers.get("content-type")?.toLowerCase() ?? ""
      if (!response.ok) {
        let code = "api_request_failed"
        if (contentType.includes("application/json")) {
          const raw = await readBoundedBody(
            response,
            maximumErrorResponseBytes,
            () => controller.abort(),
          )
          try {
            code = safeErrorCode(JSON.parse(raw) as unknown)
          } catch {
            code = "api_request_failed"
          }
        } else {
          await response.body?.cancel()
        }
        throw new ApiError(response.status, code)
      }
      if (
        response.status === 204 ||
        response.status === 205 ||
        response.headers.get("content-length") === "0"
      ) {
        await response.body?.cancel()
        return undefined as T
      }
      if (init.responseMode === "stream") {
        if (!response.body) {
          cleanup()
          return undefined as T
        }
        return wrapStream(response.body) as T
      }
      if (!contentType.includes("application/json")) {
        await response.body?.cancel()
        throw new ApiError(502, "api_content_type_invalid")
      }
      const raw = await readBoundedBody(
        response,
        maximumJsonResponseBytes,
        () => controller.abort(),
      )
      try {
        return JSON.parse(raw) as T
      } catch {
        throw new ApiError(502, "api_response_invalid")
      }
    } catch (error) {
      if (error instanceof ApiError) throw error
      throw transportError()
    } finally {
      if (init.responseMode !== "stream") cleanup()
    }
  }
}

export async function apiFetch<T>(
  path: string,
  tenantId: string,
  init: ApiInit = {},
): Promise<T> {
  const baseUrl = process.env.API_BASE_URL
  if (!baseUrl) throw new Error("api_base_url_invalid")
  const { readServerAccessToken } = await import("@/lib/auth")
  return createApiFetcher({
    apiBaseUrl: baseUrl,
    environment: process.env.NODE_ENV ?? "development",
    fetchImpl: fetch,
    getAccessToken: readServerAccessToken,
  })(path, tenantId, init)
}
