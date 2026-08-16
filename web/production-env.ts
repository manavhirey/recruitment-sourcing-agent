type Environment = { readonly [name: string]: string | undefined }

export function isStrongAuthSecret(value: string | undefined): boolean {
  return Boolean(
    value &&
    value.length >= 43 &&
    /^[A-Za-z0-9+/_-]+={0,2}$/.test(value) &&
    !/(?:replace|change|example|secret)/i.test(value),
  )
}

function secureUrl(value: string | undefined, baseOnly: boolean): boolean {
  if (!value) return false
  try {
    const url = new URL(value)
    return (
      url.protocol === "https:" &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash &&
      (!baseOnly || url.pathname === "/")
    )
  } catch {
    return false
  }
}

export function assertProductionEnvironment(environment: Environment): void {
  if (
    !isStrongAuthSecret(environment.AUTH_SECRET) ||
    !secureUrl(environment.AUTH_URL, false) ||
    !secureUrl(environment.OIDC_ISSUER, false) ||
    !secureUrl(environment.OIDC_AUDIENCE, false) ||
    !secureUrl(environment.API_BASE_URL, true) ||
    !environment.OIDC_CLIENT_ID ||
    !environment.OIDC_CLIENT_SECRET ||
    (environment.OIDC_AUDIENCE_COMPATIBILITY !== undefined &&
      !["true", "false"].includes(environment.OIDC_AUDIENCE_COMPATIBILITY))
  ) {
    throw new Error("production_configuration_invalid")
  }
}
