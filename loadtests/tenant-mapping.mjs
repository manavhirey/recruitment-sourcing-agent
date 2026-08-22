export const TENANT_COUNT = 25
export const USERS_PER_TENANT = 10
export const TOTAL_USERS = TENANT_COUNT * USERS_PER_TENANT

export function assignmentForVu(vuId) {
  if (!Number.isInteger(vuId) || vuId < 1 || vuId > TOTAL_USERS) {
    throw new RangeError(`VU id must be between 1 and ${TOTAL_USERS}`)
  }
  const userNumber = vuId - 1
  return {
    tenantNumber: Math.floor(userNumber / USERS_PER_TENANT),
    userNumber: userNumber % USERS_PER_TENANT,
  }
}

export function shouldLaunchTenantRun(vuId, iteration) {
  return iteration === 0 && assignmentForVu(vuId).userNumber === 0
}
