import { afterEach, describe, expect, it } from "vitest"

import { GET as candidateDirectory } from "@/app/api/bff/candidates/route"
import { GET as jobCandidates } from "@/app/api/bff/jobs/[jobId]/candidates/route"
import { GET as exportCsv } from "@/app/api/bff/jobs/[jobId]/export/route"

const jobId = "00000000-0000-4000-8000-000000000101"
const originalAuthUrl = process.env.AUTH_URL

afterEach(() => {
  if (originalAuthUrl === undefined) delete process.env.AUTH_URL
  else process.env.AUTH_URL = originalAuthUrl
})

describe("direct BFF route errors", () => {
  it.each([
    () => candidateDirectory(new Request("https://sourcing.example.test/api/bff/candidates?limit=101")),
    () => jobCandidates(
      new Request(`https://sourcing.example.test/api/bff/jobs/${jobId}/candidates?limit=101`),
      { params: Promise.resolve({ jobId }) },
    ),
    () => exportCsv(
      new Request("https://sourcing.example.test/api/bff/jobs/invalid/export"),
      { params: Promise.resolve({ jobId: "invalid" }) },
    ),
  ])("marks local validation failures private and no-store", async (call) => {
    const response = await call()

    expect(response.headers.get("Cache-Control")).toBe("private, no-store")
  })

  it("marks missing export authentication configuration private and no-store", async () => {
    delete process.env.AUTH_URL
    const response = await exportCsv(
      new Request(`https://sourcing.example.test/api/bff/jobs/${jobId}/export`),
      { params: Promise.resolve({ jobId }) },
    )

    expect(response.status).toBe(503)
    expect(response.headers.get("Cache-Control")).toBe("private, no-store")
  })
})
