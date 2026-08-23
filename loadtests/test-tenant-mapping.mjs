import assert from "node:assert/strict"

import {
  TENANT_COUNT,
  TOTAL_USERS,
  assignmentForVu,
  shouldLaunchTenantRun,
} from "./tenant-mapping.mjs"

const assignments = Array.from({ length: TOTAL_USERS }, (_, index) =>
  assignmentForVu(index + 1),
)
const launches = assignments
  .map((assignment, index) => ({ assignment, vuId: index + 1 }))
  .filter(({ vuId }) => shouldLaunchTenantRun(vuId, 0))

assert.equal(new Set(assignments.map(({ tenantNumber }) => tenantNumber)).size, TENANT_COUNT)
assert.equal(launches.length, TENANT_COUNT)
assert.deepEqual(
  launches.map(({ assignment }) => assignment.tenantNumber),
  Array.from({ length: TENANT_COUNT }, (_, index) => index),
)
assert.ok(launches.every(({ assignment }) => assignment.userNumber === 0))
assert.ok(launches.every(({ vuId }) => !shouldLaunchTenantRun(vuId, 1)))
assert.throws(() => assignmentForVu(0), RangeError)
assert.throws(() => assignmentForVu(TOTAL_USERS + 1), RangeError)

console.log("tenant_mapping_contract=25_distinct_launches")
