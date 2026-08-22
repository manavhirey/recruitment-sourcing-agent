import { spawnSync } from "node:child_process"

import { describe, expect, it } from "vitest"

describe("standalone production startup", () => {
  it("exits before starting Next.js when the developer override is enabled", () => {
    const result = spawnSync(process.execPath, ["start-standalone.mjs"], {
      cwd: process.cwd(),
      env: {
        ...process.env,
        NODE_ENV: "production",
        ENABLE_DEV_AUTH_OVERRIDE: "true",
      },
      encoding: "utf8",
      timeout: 5_000,
    })

    expect(result.status).toBe(1)
    expect(result.stderr).toContain("production_configuration_invalid")
    expect(result.stderr).not.toContain("MODULE_NOT_FOUND")
  })
})
