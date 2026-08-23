import { afterEach, describe, expect, it } from "vitest"

import { POST } from "@/app/api/bff/jobs/[jobId]/export/route"

const jobId = "00000000-0000-4000-8000-000000000101"
const originalAuthUrl = process.env.AUTH_URL

afterEach(() => {
  if (originalAuthUrl === undefined) delete process.env.AUTH_URL
  else process.env.AUTH_URL = originalAuthUrl
})

function formRequest(origin: string, body: ReadableStream<Uint8Array>) {
  return new Request(`https://sourcing.example.com/api/bff/jobs/${jobId}/export`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      Origin: origin,
      "Sec-Fetch-Site": origin === "https://sourcing.example.com" ? "same-origin" : "cross-site",
    },
    body,
    duplex: "half",
  } as RequestInit & { duplex: "half" })
}

describe("CSV form adapter boundary", () => {
  it("rejects cross-origin before consuming request bytes", async () => {
    process.env.AUTH_URL = "https://sourcing.example.com"
    let bytesSent = 0
    const request = formRequest("https://evil.example", new ReadableStream({
      pull(controller) {
        bytesSent += 1
        controller.enqueue(new Uint8Array([65]))
        controller.close()
      },
    }, { highWaterMark: 0 }))

    const response = await POST(request, { params: Promise.resolve({ jobId }) })

    expect(response.status).toBe(403)
    expect(bytesSent).toBe(0)
  })

  it("cancels a chunked form immediately after the one-kibibyte bound", async () => {
    process.env.AUTH_URL = "https://sourcing.example.com"
    let pulls = 0
    let cancelled = false
    const request = formRequest("https://sourcing.example.com", new ReadableStream({
      pull(controller) {
        pulls += 1
        controller.enqueue(new Uint8Array(600).fill(65))
        if (pulls === 4) controller.close()
      },
      cancel() {
        cancelled = true
      },
    }, { highWaterMark: 0 }))

    const response = await POST(request, { params: Promise.resolve({ jobId }) })

    expect(response.status).toBe(413)
    expect(pulls).toBeLessThanOrEqual(2)
    expect(cancelled).toBe(true)
  })
})
