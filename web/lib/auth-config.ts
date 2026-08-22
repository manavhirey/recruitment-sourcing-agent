import "server-only"

import type { NextAuthConfig, Session } from "next-auth"
import type { JWT } from "next-auth/jwt"
import { z } from "zod"

import { isStrongAuthSecret } from "@/production-env"

export type TenantOption = { id: string; name: string }

export type ServerJwt = JWT & {
  providerAccessToken?: string
  providerExpiresAt?: number
  tenantOptions?: TenantOption[]
}

type AuthEnvironment = Partial<
  Record<
    | "AUTH_SECRET"
    | "AUTH_URL"
    | "OIDC_ISSUER"
    | "OIDC_AUDIENCE"
    | "OIDC_AUDIENCE_COMPATIBILITY"
    | "OIDC_CLIENT_ID"
    | "OIDC_CLIENT_SECRET"
    | "NODE_ENV",
    string
  >
>

const authEnvironmentSchema = z.object({
  AUTH_SECRET: z.string().refine(isStrongAuthSecret),
  AUTH_URL: z.url(),
  OIDC_ISSUER: z.url(),
  OIDC_AUDIENCE: z.url(),
  OIDC_AUDIENCE_COMPATIBILITY: z.enum(["true", "false"]).default("false"),
  OIDC_CLIENT_ID: z.string().min(1),
  OIDC_CLIENT_SECRET: z.string().min(1),
  NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
})

const uuidSchema = z.uuid()

function secureUrl(value: string, environment: string): URL {
  const url = new URL(value)
  if (url.username || url.password || url.search || url.hash) {
    throw new Error("authentication_configuration_invalid")
  }
  if (environment === "production" && url.protocol !== "https:") {
    throw new Error("authentication_configuration_invalid")
  }
  return url
}

function tenantOptionsFromProfile(profile: unknown): TenantOption[] {
  if (!profile || typeof profile !== "object") return []
  const value = (profile as Record<string, unknown>).tenants
  if (!Array.isArray(value)) return []
  const options: TenantOption[] = []
  for (const item of value) {
    if (!item || typeof item !== "object") continue
    const { id, name } = item as Record<string, unknown>
    if (
      typeof id === "string" &&
      uuidSchema.safeParse(id).success &&
      typeof name === "string" &&
      name.trim().length > 0 &&
      name.length <= 120
    ) {
      options.push({ id, name: name.trim() })
    }
  }
  return options
}

export function withProviderTokens(
  token: JWT,
  account: {
    access_token?: string
    refresh_token?: string
    expires_at?: number
  },
  profile?: unknown,
): ServerJwt {
  if (
    !account.access_token ||
    !Number.isSafeInteger(account.expires_at) ||
    (account.expires_at ?? 0) <= 0
  ) {
    throw new Error("oidc_callback_invalid")
  }
  return {
    ...token,
    providerAccessToken: account.access_token,
    providerExpiresAt: account.expires_at,
    tenantOptions: tenantOptionsFromProfile(profile),
  }
}

export function publicSession(session: Session): Session {
  return {
    expires: session.expires,
    user: session.user
      ? {
          name: session.user.name ?? null,
          email: session.user.email ?? null,
          image: session.user.image ?? null,
        }
      : undefined,
  }
}

export function serverAccessToken(token: ServerJwt | null, nowMs = Date.now()): string {
  if (
    !token?.providerAccessToken ||
    token.providerExpiresAt === undefined ||
    token.providerExpiresAt * 1000 <= nowMs
  ) {
    throw new Error("unauthenticated")
  }
  return token.providerAccessToken
}

export function buildAuthOptions(
  environment: AuthEnvironment = process.env,
): NextAuthConfig {
  const parsed = authEnvironmentSchema.safeParse(environment)
  if (!parsed.success) throw new Error("authentication_configuration_invalid")
  const values = parsed.data
  const issuer = secureUrl(values.OIDC_ISSUER, values.NODE_ENV)
  secureUrl(values.OIDC_AUDIENCE, values.NODE_ENV)
  const authUrl = secureUrl(values.AUTH_URL, values.NODE_ENV)
  const issuerBase = issuer.href.endsWith("/") ? issuer.href : `${issuer.href}/`
  const secureCookies = authUrl.protocol === "https:"
  const authorizationParams: Record<string, string> = {
    scope: "openid email profile",
    resource: values.OIDC_AUDIENCE,
  }
  if (values.OIDC_AUDIENCE_COMPATIBILITY === "true") {
    authorizationParams.audience = values.OIDC_AUDIENCE
  }

  return {
    secret: values.AUTH_SECRET,
    session: { strategy: "jwt", maxAge: 8 * 60 * 60 },
    jwt: { maxAge: 8 * 60 * 60 },
    useSecureCookies: secureCookies,
    cookies: {
      sessionToken: {
        name: secureCookies
          ? "__Secure-next-auth.session-token"
          : "next-auth.session-token",
        options: {
          httpOnly: true,
          sameSite: "lax",
          path: "/",
          secure: secureCookies,
        },
      },
    },
    providers: [
      {
        id: "oidc",
        name: "Company identity",
        type: "oidc",
        issuer: issuer.href,
        wellKnown: new URL(".well-known/openid-configuration", issuerBase).href,
        clientId: values.OIDC_CLIENT_ID,
        clientSecret: values.OIDC_CLIENT_SECRET,
        checks: ["pkce", "state"],
        authorization: { params: authorizationParams },
        profile(profile) {
          if (
            typeof profile.sub !== "string" ||
            typeof profile.email !== "string"
          ) {
            throw new Error("oidc_profile_invalid")
          }
          return {
            id: profile.sub,
            name: typeof profile.name === "string" ? profile.name : null,
            email: profile.email,
            image: null,
          }
        },
      },
    ],
    callbacks: {
      async jwt({ token, account, profile }) {
        if (!account) return token
        return withProviderTokens(token, account, profile)
      },
      async session({ session }) {
        return publicSession(session)
      },
    },
    pages: { error: "/auth/error" },
  }
}
