import "server-only"

import {
  createCipheriv,
  createDecipheriv,
  createHash,
  createHmac,
  randomBytes,
} from "node:crypto"

import { NextResponse } from "next/server"

const lifetimeSeconds = 10 * 60
const domain = "recruitment-sourcing:invitation-cookie:v1"
const tokenPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.[A-Za-z0-9_-]{43}$/i

type Environment = "development" | "test" | "production"

function key(secret: string): Buffer {
  if (secret.length < 32) throw new Error("authentication_configuration_invalid")
  return createHash("sha256").update(`${domain}\0${secret}`).digest()
}

function tenantId(token: string): string | null {
  if (!tokenPattern.test(token)) return null
  return token.slice(0, token.indexOf("."))
}

export function invitationCookieSettings(environment: Environment) {
  const secure = environment === "production"
  return {
    name: secure ? "__Host-sourcing-invitation" : "sourcing-invitation",
    options: {
      httpOnly: true,
      sameSite: "lax" as const,
      secure,
      path: "/",
      maxAge: lifetimeSeconds,
    },
  }
}

export function sealInvitationToken(
  token: string,
  secret: string,
  nowSeconds = Math.floor(Date.now() / 1_000),
): string {
  if (!tenantId(token)) throw new Error("invitation_invalid")
  const nonce = randomBytes(12)
  const cipher = createCipheriv("aes-256-gcm", key(secret), nonce)
  cipher.setAAD(Buffer.from(domain))
  const payload = Buffer.from(JSON.stringify({ token, expires: nowSeconds + lifetimeSeconds }))
  const encrypted = Buffer.concat([cipher.update(payload), cipher.final()])
  return `v1.${nonce.toString("base64url")}.${Buffer.concat([encrypted, cipher.getAuthTag()]).toString("base64url")}`
}

export function unsealInvitationToken(
  value: string,
  secret: string,
  nowSeconds = Math.floor(Date.now() / 1_000),
): string | null {
  try {
    const [version, nonceText, encryptedText, extra] = value.split(".")
    if (version !== "v1" || extra !== undefined) return null
    const nonce = Buffer.from(nonceText, "base64url")
    const combined = Buffer.from(encryptedText, "base64url")
    if (nonce.length !== 12 || combined.length <= 16) return null
    const decipher = createDecipheriv("aes-256-gcm", key(secret), nonce)
    decipher.setAAD(Buffer.from(domain))
    decipher.setAuthTag(combined.subarray(combined.length - 16))
    const decoded = Buffer.concat([
      decipher.update(combined.subarray(0, combined.length - 16)),
      decipher.final(),
    ]).toString("utf8")
    const payload = JSON.parse(decoded) as unknown
    if (!payload || typeof payload !== "object") return null
    const token = (payload as Record<string, unknown>).token
    const expires = (payload as Record<string, unknown>).expires
    if (
      typeof token !== "string" ||
      !tenantId(token) ||
      typeof expires !== "number" ||
      !Number.isSafeInteger(expires) ||
      expires <= nowSeconds ||
      expires > nowSeconds + lifetimeSeconds
    ) return null
    return token
  } catch {
    return null
  }
}

function responseHeaders() {
  return {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
  }
}

function redirectResponse(request: Request, path: string): NextResponse {
  return NextResponse.redirect(new URL(path, request.url), {
    status: 303,
    headers: responseHeaders(),
  })
}

function clearInvitationCookie(response: NextResponse, environment: Environment) {
  const cookie = invitationCookieSettings(environment)
  response.cookies.set(cookie.name, "", { ...cookie.options, maxAge: 0 })
}

export function captureInvitation(
  token: string,
  options: {
    secret: string
    environment: Environment
    nowSeconds?: number
  },
): NextResponse {
  const validTenant = tenantId(token)
  if (!validTenant) {
    return NextResponse.json(
      { code: "invitation_invalid" },
      { status: 400, headers: responseHeaders() },
    )
  }
  const response = NextResponse.json(
    { next: "/invite/claim" },
    { headers: responseHeaders() },
  )
  const cookie = invitationCookieSettings(options.environment)
  response.cookies.set(
    cookie.name,
    sealInvitationToken(token, options.secret, options.nowSeconds),
    cookie.options,
  )
  return response
}

export async function claimCapturedInvitation(
  request: Request,
  dependencies: {
    secret: string
    environment: Environment
    nowSeconds?: number
    readCookie: () => string | undefined
    hasSession: () => Promise<boolean>
    claim: (
      token: string,
      tenantId: string,
      idempotencyKey: string,
    ) => Promise<{ tenant_id: string }>
    selectTenant: (tenantId: string) => Promise<void> | void
  },
): Promise<NextResponse> {
  const sealed = dependencies.readCookie()
  const token = sealed
    ? unsealInvitationToken(sealed, dependencies.secret, dependencies.nowSeconds)
    : null
  if (!token) {
    const invalid = redirectResponse(request, "/auth/error?error=InvitationInvalid")
    clearInvitationCookie(invalid, dependencies.environment)
    return invalid
  }
  let authenticated: boolean
  try {
    authenticated = await dependencies.hasSession()
  } catch {
    const failed = redirectResponse(request, "/auth/error?error=InvitationInvalid")
    clearInvitationCookie(failed, dependencies.environment)
    return failed
  }
  if (!authenticated) {
    return redirectResponse(
      request,
      "/api/auth/signin?callbackUrl=%2Finvite%2Fclaim",
    )
  }
  const invitationTenantId = tenantId(token)
  if (!invitationTenantId) {
    const invalid = redirectResponse(request, "/auth/error?error=InvitationInvalid")
    clearInvitationCookie(invalid, dependencies.environment)
    return invalid
  }
  const idempotencyKey = `invite-claim-${createHmac("sha256", key(dependencies.secret)).update(token).digest("base64url")}`
  try {
    const result = await dependencies.claim(
      token,
      invitationTenantId,
      idempotencyKey,
    )
    await dependencies.selectTenant(result.tenant_id)
    const complete = redirectResponse(request, "/jobs")
    clearInvitationCookie(complete, dependencies.environment)
    return complete
  } catch {
    const failed = redirectResponse(request, "/auth/error?error=InvitationInvalid")
    clearInvitationCookie(failed, dependencies.environment)
    return failed
  }
}
