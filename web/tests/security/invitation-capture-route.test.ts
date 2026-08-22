import { afterEach, describe, expect, it } from "vitest"

import { POST } from "@/app/invite/capture/route"

const originalAuthUrl = process.env.AUTH_URL
const originalAuthSecret = process.env.AUTH_SECRET
const appUrl = "https://sourcing.example.com"

afterEach(() => {
  if (originalAuthUrl === undefined) delete process.env.AUTH_URL
  else process.env.AUTH_URL = originalAuthUrl
  if (originalAuthSecret === undefined) delete process.env.AUTH_SECRET
  else process.env.AUTH_SECRET = originalAuthSecret
})

function captureRequest(origin: string, body: ReadableStream<Uint8Array>) {
  return new Request(`${appUrl}/invite/capture`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: origin,
      "Sec-Fetch-Site": origin === appUrl ? "same-origin" : "cross-site",
    },
    body,
    duplex: "half",
  } as RequestInit & { duplex: "half" })
}

function configure() {
  process.env.AUTH_URL = appUrl
  process.env.AUTH_SECRET = "a-production-strength-auth-secret-value"
}

describe("invitation capture boundary", () => {
  it("rejects cross-origin before consuming any token bytes", async () => {
    configure()
    let pulls = 0
    const request = captureRequest("https://evil.example", new ReadableStream({
      pull(controller) {
        pulls += 1
        controller.enqueue(new Uint8Array([65]))
        controller.close()
      },
    }, { highWaterMark: 0 }))

    const response = await POST(request)

    expect(response.status).toBe(403)
    expect(pulls).toBe(0)
  })

  it("cancels a chunked body immediately after the 512-byte bound", async () => {
    configure()
    let pulls = 0
    let cancelled = false
    const request = captureRequest(appUrl, new ReadableStream({
      pull(controller) {
        pulls += 1
        controller.enqueue(new Uint8Array(300).fill(65))
        if (pulls === 4) controller.close()
      },
      cancel() {
        cancelled = true
      },
    }, { highWaterMark: 0 }))

    const response = await POST(request)

    expect(response.status).toBe(413)
    expect(pulls).toBeLessThanOrEqual(2)
    expect(cancelled).toBe(true)
  })
})
