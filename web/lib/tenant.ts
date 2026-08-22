import "server-only"

import { createHmac, timingSafeEqual } from "node:crypto"

import { cookies } from "next/headers"
import { z } from "zod"

import type { TenantOption } from "@/lib/auth-config"
import { requestDevelopmentAuthOverride } from "@/lib/dev-auth"

const COOKIE_LIFETIME_SECONDS = 7 * 24 * 60 * 60
const COOKIE_SIGNING_DOMAIN = "recruitment-sourcing:selected-tenant:v1"
const tenantOptionSchema = z.object({
  id: z.uuid(),
  name: z.string().trim().min(1).max(120),
})
const uuidSchema = z.uuid()

function signingKey(secret: string): Buffer {
  return createHmac("sha256", secret).update(COOKIE_SIGNING_DOMAIN).digest()
}

function signature(payload: string, secret: string): Buffer {
  return createHmac("sha256", signingKey(secret)).update(payload).digest()
}

export function signTenantSelection(
  tenantId: string,
  secret: string,
  nowSeconds = Math.floor(Date.now() / 1000),
): string {
  if (!uuidSchema.safeParse(tenantId).success || secret.length < 32) {
    throw new Error("tenant_selection_invalid")
  }
  const payload = `v1.${tenantId}.${nowSeconds + COOKIE_LIFETIME_SECONDS}`
  return `${payload}.${signature(payload, secret).toString("base64url")}`
}

export function verifyTenantSelection(
  value: string,
  secret: string,
  nowSeconds = Math.floor(Date.now() / 1000),
): string | null {
  const parts = value.split(".")
  if (parts.length !== 4 || parts[0] !== "v1" || secret.length < 32) return null
  const [, tenantId, expiresText, suppliedText] = parts
  const expires = Number(expiresText)
  if (
    !uuidSchema.safeParse(tenantId).success ||
    !Number.isSafeInteger(expires) ||
    expires <= nowSeconds ||
    expires > nowSeconds + COOKIE_LIFETIME_SECONDS
  ) {
    return null
  }
  let supplied: Buffer
  try {
    supplied = Buffer.from(suppliedText, "base64url")
  } catch {
    return null
  }
  const expected = signature(`v1.${tenantId}.${expires}`, secret)
  if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
    return null
  }
  return tenantId
}

export function assertMutationOrigin(request: Request, appUrl: string): void {
  const expected = new URL(appUrl).origin
  const origin = request.headers.get("Origin")
  const fetchSite = request.headers.get("Sec-Fetch-Site")
  if (
    origin !== expected ||
    (fetchSite !== null && fetchSite !== "same-origin")
  ) {
    throw new Error("invalid_request_origin")
  }
}

export function allowedTenantOptions(
  verifiedOidcOptions: unknown,
  configuredJson: string | undefined,
): TenantOption[] {
  const candidates: unknown[] = Array.isArray(verifiedOidcOptions)
    ? [...verifiedOidcOptions]
    : []
  if (configuredJson) {
    try {
      const configured = JSON.parse(configuredJson)
      if (Array.isArray(configured)) candidates.push(...configured)
    } catch {
      throw new Error("tenant_options_configuration_invalid")
    }
  }
  const result: TenantOption[] = []
  const seen = new Set<string>()
  for (const candidate of candidates) {
    const parsed = tenantOptionSchema.safeParse(candidate)
    if (parsed.success && !seen.has(parsed.data.id)) {
      seen.add(parsed.data.id)
      result.push(parsed.data)
    }
  }
  return result
}

export function tenantCookieSettings(environment = process.env.NODE_ENV) {
  const secure = environment === "production"
  return {
    name: secure ? "__Host-sourcing-tenant" : "sourcing-tenant",
    options: {
      httpOnly: true,
      sameSite: "strict" as const,
      secure,
      path: "/",
      maxAge: COOKIE_LIFETIME_SECONDS,
    },
  }
}

export async function selectedTenantId(): Promise<string | null> {
  const developmentOverride = await requestDevelopmentAuthOverride()
  if (developmentOverride) return developmentOverride.tenantId
  const secret = process.env.AUTH_SECRET
  if (!secret) throw new Error("authentication_configuration_invalid")
  const cookie = tenantCookieSettings()
  const value = (await cookies()).get(cookie.name)?.value
  return value ? verifyTenantSelection(value, secret) : null
}

export async function setSelectedTenant(tenantId: string): Promise<void> {
  const secret = process.env.AUTH_SECRET
  if (!secret) throw new Error("authentication_configuration_invalid")
  const cookie = tenantCookieSettings()
  ;(await cookies()).set(
    cookie.name,
    signTenantSelection(tenantId, secret),
    cookie.options,
  )
}

export async function clearSelectedTenant(): Promise<void> {
  const cookie = tenantCookieSettings()
  ;(await cookies()).set(cookie.name, "", { ...cookie.options, maxAge: 0 })
}
