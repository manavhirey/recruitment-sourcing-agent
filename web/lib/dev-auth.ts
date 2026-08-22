import "server-only"

import { headers } from "next/headers"
import type { Session } from "next-auth"

import type { ServerJwt, TenantOption } from "@/lib/auth-config"

export const DEV_TENANT: TenantOption = {
  id: "00000000-0000-4000-8000-000000000001",
  name: "E2E Agency",
}

const DEV_ACCESS_TOKEN = "e2e-access-token"
const DEV_SESSION_SECONDS = 60 * 60

type DevAuthEnvironment = {
  readonly ENABLE_DEV_AUTH_OVERRIDE?: string
  readonly NODE_ENV?: string
}

export type DevAuthOverride = {
  session: Session
  token: ServerJwt
  tenantId: string
}

function loopbackHostname(host: string | null): boolean {
  if (!host) return false
  const match = /^(?:localhost|127\.0\.0\.1|\[::1\])(?::([0-9]{1,5}))?$/i.exec(host)
  if (!match) return false
  const port = match[1]
  return port === undefined || (Number(port) >= 1 && Number(port) <= 65_535)
}

export function developmentAuthOverride(
  environment: DevAuthEnvironment,
  host: string | null,
  nowSeconds = Math.floor(Date.now() / 1_000),
): DevAuthOverride | null {
  if (
    environment.ENABLE_DEV_AUTH_OVERRIDE !== "true" ||
    !["development", "test"].includes(environment.NODE_ENV ?? "") ||
    !loopbackHostname(host)
  ) {
    return null
  }

  const expires = nowSeconds + DEV_SESSION_SECONDS
  return {
    session: {
      expires: new Date(expires * 1_000).toISOString(),
      user: {
        name: "Local Developer",
        email: "developer@example.test",
        image: null,
      },
    },
    token: {
      sub: "developer|local-owner",
      name: "Local Developer",
      email: "developer@example.test",
      providerAccessToken: DEV_ACCESS_TOKEN,
      providerExpiresAt: expires,
      tenantOptions: [DEV_TENANT],
    },
    tenantId: DEV_TENANT.id,
  }
}

export async function requestDevelopmentAuthOverride(): Promise<DevAuthOverride | null> {
  const requestHeaders = await headers()
  return developmentAuthOverride(process.env, requestHeaders.get("host"))
}
