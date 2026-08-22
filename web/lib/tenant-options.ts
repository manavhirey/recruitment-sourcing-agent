import "server-only"

import { ApiError } from "@/lib/api"
import type { TenantOption } from "@/lib/auth-config"

export async function membershipValidatedTenantOptions(
  options: readonly TenantOption[],
  validateMembership: (tenantId: string) => Promise<void>,
): Promise<TenantOption[]> {
  const decisions = await Promise.all(
    options.map(async (option) => {
      try {
        await validateMembership(option.id)
        return option
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null
        throw error
      }
    }),
  )
  return decisions.filter((option): option is TenantOption => option !== null)
}
