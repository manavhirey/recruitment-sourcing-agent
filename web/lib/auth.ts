import "server-only"

import { headers } from "next/headers"
import { NextRequest } from "next/server"
import NextAuth, { type Session } from "next-auth"
import { getToken } from "next-auth/jwt"

import {
  buildAuthOptions,
  serverAccessToken,
  type ServerJwt,
} from "@/lib/auth-config"

export {
  buildAuthOptions,
  publicSession,
  serverAccessToken,
  withProviderTokens,
} from "@/lib/auth-config"
export type { ServerJwt, TenantOption } from "@/lib/auth-config"

export async function auth(): Promise<Session | null> {
  return NextAuth(buildAuthOptions()).auth()
}

export async function readServerJwt(): Promise<ServerJwt | null> {
  const options = buildAuthOptions()
  const requestHeaders = new Headers(await headers())
  const request = new NextRequest(process.env.AUTH_URL!, {
    headers: requestHeaders,
  })
  return (await getToken({
    req: request,
    secret: options.secret,
    secureCookie: options.useSecureCookies,
    cookieName: options.cookies?.sessionToken?.name,
  })) as ServerJwt | null
}

export async function readServerAccessToken(): Promise<string> {
  return serverAccessToken(await readServerJwt())
}

export function authRouteHandlers() {
  return NextAuth(buildAuthOptions()).handlers
}
