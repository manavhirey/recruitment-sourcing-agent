import type { NextConfig } from "next"

import { assertProductionEnvironment } from "./production-env"

if (process.env.NODE_ENV === "production") {
  assertProductionEnvironment(process.env)
}

export function contentSecurityPolicyFor(oidcIssuer = process.env.OIDC_ISSUER) {
  const oidcOrigin = new URL(oidcIssuer ?? "https://identity.build.invalid/").origin
  return [
    "default-src 'self'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
    `form-action 'self' ${oidcOrigin}`,
    "img-src 'self' data:",
    "font-src 'self'",
    "object-src 'none'",
    `script-src 'self' 'unsafe-inline'${process.env.NODE_ENV === "production" ? "" : " 'unsafe-eval'"}`,
    "style-src 'self' 'unsafe-inline'",
    "connect-src 'self'",
  ].join("; ")
}

const contentSecurityPolicy = contentSecurityPolicyFor()

const nextConfig: NextConfig = {
  agentRules: false,
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "Content-Security-Policy", value: contentSecurityPolicy },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ]
  },
}

export default nextConfig
